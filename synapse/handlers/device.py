# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from synapse.api import errors
from synapse.util import stringutils
from synapse.util.async import Linearizer
from synapse.types import get_domain_from_id
from twisted.internet import defer
from ._base import BaseHandler

import logging

logger = logging.getLogger(__name__)


class DeviceHandler(BaseHandler):
    def __init__(self, hs):
        super(DeviceHandler, self).__init__(hs)

        self.hs = hs
        self.state = hs.get_state_handler()
        self.federation_sender = hs.get_federation_sender()
        self.federation = hs.get_replication_layer()
        self._remote_edue_linearizer = Linearizer(name="remote_device_list")

        self.federation.register_edu_handler(
            "m.device_list_update", self._incoming_device_list_update,
        )
        self.federation.register_query_handler(
            "user_devices", self.on_federation_query_user_devices,
        )

    @defer.inlineCallbacks
    def check_device_registered(self, user_id, device_id,
                                initial_device_display_name=None):
        """
        If the given device has not been registered, register it with the
        supplied display name.

        If no device_id is supplied, we make one up.

        Args:
            user_id (str):  @user:id
            device_id (str | None): device id supplied by client
            initial_device_display_name (str | None): device display name from
                 client
        Returns:
            str: device id (generated if none was supplied)
        """
        if device_id is not None:
            new_device = yield self.store.store_device(
                user_id=user_id,
                device_id=device_id,
                initial_device_display_name=initial_device_display_name,
            )
            if new_device:
                yield self.notify_device_update(user_id, [device_id])
            defer.returnValue(device_id)

        # if the device id is not specified, we'll autogen one, but loop a few
        # times in case of a clash.
        attempts = 0
        while attempts < 5:
            device_id = stringutils.random_string(10).upper()
            new_device = yield self.store.store_device(
                user_id=user_id,
                device_id=device_id,
                initial_device_display_name=initial_device_display_name,
            )
            if new_device:
                yield self.notify_device_update(user_id, [device_id])
                defer.returnValue(device_id)
            attempts += 1

        raise errors.StoreError(500, "Couldn't generate a device ID.")

    @defer.inlineCallbacks
    def get_devices_by_user(self, user_id):
        """
        Retrieve the given user's devices

        Args:
            user_id (str):
        Returns:
            defer.Deferred: list[dict[str, X]]: info on each device
        """

        device_map = yield self.store.get_devices_by_user(user_id)

        ips = yield self.store.get_last_client_ip_by_device(
            devices=((user_id, device_id) for device_id in device_map.keys())
        )

        devices = device_map.values()
        for device in devices:
            _update_device_from_client_ips(device, ips)

        defer.returnValue(devices)

    @defer.inlineCallbacks
    def get_device(self, user_id, device_id):
        """ Retrieve the given device

        Args:
            user_id (str):
            device_id (str):

        Returns:
            defer.Deferred: dict[str, X]: info on the device
        Raises:
            errors.NotFoundError: if the device was not found
        """
        try:
            device = yield self.store.get_device(user_id, device_id)
        except errors.StoreError:
            raise errors.NotFoundError
        ips = yield self.store.get_last_client_ip_by_device(
            devices=((user_id, device_id),)
        )
        _update_device_from_client_ips(device, ips)
        defer.returnValue(device)

    @defer.inlineCallbacks
    def delete_device(self, user_id, device_id):
        """ Delete the given device

        Args:
            user_id (str):
            device_id (str):

        Returns:
            defer.Deferred:
        """

        try:
            yield self.store.delete_device(user_id, device_id)
        except errors.StoreError, e:
            if e.code == 404:
                # no match
                pass
            else:
                raise

        yield self.store.user_delete_access_tokens(
            user_id, device_id=device_id,
            delete_refresh_tokens=True,
        )

        yield self.store.delete_e2e_keys_by_device(
            user_id=user_id, device_id=device_id
        )

        yield self.notify_device_update(user_id, [device_id])

    @defer.inlineCallbacks
    def update_device(self, user_id, device_id, content):
        """ Update the given device

        Args:
            user_id (str):
            device_id (str):
            content (dict): body of update request

        Returns:
            defer.Deferred:
        """

        try:
            yield self.store.update_device(
                user_id,
                device_id,
                new_display_name=content.get("display_name")
            )
            yield self.notify_device_update(user_id, [device_id])
        except errors.StoreError, e:
            if e.code == 404:
                raise errors.NotFoundError()
            else:
                raise

    @defer.inlineCallbacks
    def notify_device_update(self, user_id, device_ids):
        """Notify that a user's device(s) has changed. Pokes the notifier, and
        remote servers if the user is local.
        """
        rooms = yield self.store.get_rooms_for_user(user_id)
        room_ids = [r.room_id for r in rooms]

        hosts = set()
        if self.hs.is_mine_id(user_id):
            for room_id in room_ids:
                users = yield self.state.get_current_user_in_room(room_id)
                hosts.update(get_domain_from_id(u) for u in users)
            hosts.discard(self.server_name)

        position = yield self.store.add_device_change_to_streams(
            user_id, device_ids, list(hosts)
        )

        yield self.notifier.on_new_event(
            "device_list_key", position, rooms=room_ids,
        )

        if hosts:
            logger.info("Sending device list update notif to: %r", hosts)
            for host in hosts:
                self.federation_sender.send_device_messages(host)

    @defer.inlineCallbacks
    def get_device_list_changes(self, user_id, room_ids, from_key):
        """For a user and their joined rooms, calculate which device updates
        we need to return.
        """
        room_ids = frozenset(room_ids)

        user_ids_changed = set()
        changed = yield self.store.get_user_whose_devices_changed(from_key)
        for other_user_id in changed:
            other_rooms = yield self.store.get_rooms_for_user(other_user_id)
            if room_ids.intersection(e.room_id for e in other_rooms):
                user_ids_changed.add(other_user_id)

        defer.returnValue(user_ids_changed)

    @defer.inlineCallbacks
    def _incoming_device_list_update(self, origin, edu_content):
        user_id = edu_content["user_id"]
        device_id = edu_content["device_id"]
        stream_id = edu_content["stream_id"]
        prev_ids = edu_content.get("prev_id", [])

        if get_domain_from_id(user_id) != origin:
            # TODO: Raise?
            logger.warning("Got device list update edu for %r from %r", user_id, origin)
            return

        logger.info("Got edu: %r", edu_content)

        with (yield self._remote_edue_linearizer.queue(user_id)):
            # If the prev id matches whats in our cache table, then we don't need
            # to resync the users device list, otherwise we do.
            resync = True
            if len(prev_ids) == 1:
                extremity = yield self.store.get_device_list_remote_extremity(user_id)
                logger.info("Extrem: %r, prev_ids: %r", extremity, prev_ids)
                if str(extremity) == str(prev_ids[0]):
                    resync = False

            if resync:
                # Fetch all devices for the user.
                result = yield self.federation.query_user_devices(origin, user_id)
                stream_id = result["stream_id"]
                devices = result["devices"]
                yield self.store.update_remote_device_list_cache(
                    user_id, devices, stream_id,
                )
                device_ids = [device["device_id"] for device in devices]
                yield self.notify_device_update(user_id, device_ids)
            else:
                # Simply update the single device, since we know that is the only
                # change (becuase of the single prev_id matching the current cache)
                content = dict(edu_content)
                for key in ("user_id", "device_id", "stream_id", "prev_ids"):
                    content.pop(key, None)
                yield self.store.update_remote_device_list_cache_entry(
                    user_id, device_id, content, stream_id,
                )
                yield self.notify_device_update(user_id, [device_id])

    @defer.inlineCallbacks
    def on_federation_query_user_devices(self, user_id):
        stream_id, devices = yield self.store.get_devices_with_keys_by_user(user_id)
        defer.returnValue({
            "user_id": user_id,
            "stream_id": stream_id,
            "devices": devices,
        })


def _update_device_from_client_ips(device, client_ips):
    ip = client_ips.get((device["user_id"], device["device_id"]), {})
    device.update({
        "last_seen_ts": ip.get("last_seen"),
        "last_seen_ip": ip.get("ip"),
    })
