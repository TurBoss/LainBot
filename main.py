#!/usr/bin/env python

import os
import sys
import io
import signal

from urllib.parse import urlparse

import magic

import asyncio
import aiofiles.os
import imagehash

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pathlib import Path
from PIL import Image
from random import choice

from nio import (AsyncClient,
                 AsyncClientConfig,
                 RoomMessageText,
                 UploadResponse,
                 RoomMessageImage,
                 RoomGetEventError,
                 SyncError,
                 SyncResponse,
                 DownloadResponse,
                 LoginResponse,
                 WhoamiResponse,
                 ReactionEvent)

from storage import Storage
from config import Config

import logging

class LainBot:
    _initial_sync_done = False

    def __init__(self, config_path):

        self.client_config = None

        self.scheduler = AsyncIOScheduler()
        self.config = Config(config_path)

        # Configure the database
        self.store = Storage(self.config.database)

        # Logging is configured by Config; do not attach a second handler here
        # or every record is printed twice through the root logger.
        self.logger = logging.getLogger("LainBot")

        self.logger.info("Initializing system.")

        self.homeserver = self.config.homeserver_url
        self.access_token = self.config.user_token
        self.user_id = self.config.user_id
        self.bot_owners = self.config.owners
        self.device_id = self.config.device_id
        self.room_id = self.config.room_id
        self.path = self.config.pics_path
        self.event_time = self.config.event_time

        self.client = None
        self.users = set()
        self.hours, self.minutes = (int(part) for part in self.event_time.split(':'))
        self.scheduler.add_job(self.job, 'cron', day_of_week='mon-sun',
                               hour=self.hours, minute=self.minutes)
        self.scheduler.start()

        self._index_existing_images()


    async def on_error(self, response):
        # A SyncError is surfaced by sync_forever(); the reconnect loop in
        # `start` owns the client lifecycle, so don't tear the client down here.
        self.logger.error("Sync error: %s", response)
        # Non-blocking backoff so a persistently failing sync (e.g. an expired
        # access token) can't spin the loop at full speed.
        await asyncio.sleep(5)

    async def on_sync(self, _response):
        if not self._initial_sync_done:
            self._initial_sync_done = True
            for room in self.client.rooms:
                self.logger.info('room %s', room)
            self.logger.info('initial sync done, ready for work')

    def _build_client(self):
        self.client_config = AsyncClientConfig(
            # None = retry forever with nio's exponential backoff. A value of
            # 0 means "give up after the first failure", which is why dropped
            # connections previously escaped to (and crashed) the outer loop.
            max_limit_exceeded=None,
            max_timeouts=None,
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        client = AsyncClient(self.homeserver, self.config.user_id,
                             device_id=self.config.device_id,
                             store_path=self.config.store_path,
                             config=self.client_config)
        client.access_token = self.access_token
        client.user_id = self.user_id
        client.device_id = self.device_id

        client.add_response_callback(self.on_error, SyncError)
        client.add_response_callback(self.on_sync, SyncResponse)
        client.add_event_callback(self.on_message, RoomMessageText)
        client.add_event_callback(self.on_reaction, ReactionEvent)
        client.add_event_callback(self.on_image, RoomMessageImage)
        return client

    async def _close_client(self):
        # A client that has been closed must not be reused; always rebuild it.
        if self.client is None:
            return
        try:
            await self.client.close()
        except Exception:
            self.logger.exception("Error while closing the Matrix client")
        finally:
            self.client = None

    async def _authenticate(self):
        """Establish credentials and make sure the client uses the correct
        device id before the E2E crypto store is created.

        With an access token the server is authoritative about which device
        the token belongs to -- if the configured device_id differs, key
        uploads fail with M_BAD_JSON.
        """
        if self.access_token:
            response = await self.client.whoami()
            if not isinstance(response, WhoamiResponse):
                raise RuntimeError(f"whoami failed: {response}")
            if not response.device_id:
                raise RuntimeError(
                    "Access token is not bound to a device; end-to-end "
                    "encryption needs a device id. Log in with a password "
                    "or provide a token that has an associated device."
                )
            # The server is authoritative: adopt its user_id/device_id so the
            # E2E crypto store and key uploads use the token's device.
            if self.config.device_id and self.config.device_id != response.device_id:
                self.logger.warning(
                    "Configured device_id %r does not match the token's device "
                    "%r; using the server's.",
                    self.config.device_id, response.device_id,
                )
            self.user_id = response.user_id
            self.device_id = response.device_id
            self.client.user_id = response.user_id
            self.client.device_id = response.device_id
            self.logger.info(
                "Authenticated as %s on device %s",
                response.user_id, response.device_id,
            )
            return

        if not self.config.user_password:
            raise RuntimeError("No access token or password configured")

        response = await self.client.login(
            password=self.config.user_password,
            device_name=self.config.device_name,
        )
        if not isinstance(response, LoginResponse):
            raise RuntimeError(f"Login failed: {response}")

        self.access_token = response.access_token
        self.client.access_token = response.access_token
        self.user_id = response.user_id
        self.logger.info(
            "Logged in as %s on device %s",
            response.user_id, response.device_id,
        )

    async def start(self):
        self.logger.info("Initializing client.")

        backoff = 5
        max_backoff = 300
        while True:
            try:
                self.client = self._build_client()
                # Re-arm the guard so events replayed during the initial sync
                # after a reconnect aren't processed as new.
                self._initial_sync_done = False

                await self._authenticate()

                # Use token to log in
                self.client.load_store()

                # Sync encryption keys with the server
                if self.client.should_upload_keys:
                    await self.client.keys_upload()

                self.logger.info("Starting sync loop")
                await self.client.sync_forever(timeout=30000)

                # sync_forever only returns on cancellation, so a normal return
                # means the connection dropped.
                self.logger.warning("Sync loop exited, reconnecting...")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.warning(
                    "Homeserver connection lost (%r); retrying in %ss...",
                    e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            finally:
                await self._close_client()

            backoff = 5
            await asyncio.sleep(backoff)

    def _index_existing_images(self):
        """Populate the image-hash index from files already on disk."""
        try:
            entries = list(Path(self.path).iterdir())
        except OSError as e:
            self.logger.warning("Unable to list image directory %s: %s", self.path, e)
            return

        indexed = 0
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                with Image.open(entry) as img:
                    file_hash = str(imagehash.average_hash(img))
                self.store.add_hash(file_hash, entry.name)
                indexed += 1
            except Exception as e:
                self.logger.debug("Skipping unreadable image %s: %s", entry, e)

        self.logger.info("Indexed %d stored image(s)", indexed)

    def _pick_random_picture(self):
        """Return a random image file, or None if there are none."""
        try:
            files = [p for p in Path(self.path).iterdir() if p.is_file()]
        except OSError as e:
            self.logger.error("Unable to read image directory %s: %s", self.path, e)
            return None

        if not files:
            self.logger.warning("No images available in %s", self.path)
            return None

        return choice(files)

    async def shutdown(self):
        self.logger.info("Shutting down")
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            self.logger.exception("Error shutting down scheduler")
        await self._close_client()
        try:
            self.store.close()
        except Exception:
            self.logger.exception("Error closing database")

    async def job(self):
        self.logger.info("Job started")
        pic = self._pick_random_picture()
        if pic is None:
            return

        self.logger.info("Upload %s", pic.name)
        await self.send_image(self.room_id, str(pic))
        self.logger.info("Job finished")

        self.users.clear()

    async def send_image(self, room, image):
        """Send image to to matrix.
        Arguments:
        ---------
        client : Client
        room_id : str
        image : str, file name of image
        This is a working example for a JPG image.
            "content": {
                "body": "someimage.jpg",
                "info": {
                    "size": 5420,
                    "mimetype": "image/jpeg",
                    "thumbnail_info": {
                        "w": 100,
                        "h": 100,
                        "mimetype": "image/jpeg",
                        "size": 2106
                    },
                    "w": 100,
                    "h": 100,
                    "thumbnail_url": "mxc://example.com/SomeStrangeThumbnailUriKey"
                },
                "msgtype": "m.image",
                "url": "mxc://example.com/SomeStrangeUriKey"
            }
        """
        if self.client is None:
            self.logger.warning("Client not connected; dropping image send for %s", image)
            return

        mime_type = magic.from_file(image, mime=True)  # e.g. "image/jpeg"
        if not mime_type.startswith("image/"):
            self.logger.warning("Drop message because file does not have an image mime type.")
            return

        im = Image.open(image)
        (width, height) = im.size  # im.size returns (width,height) tuple

        # first do an upload of image, then send URI of upload to room
        file_stat = await aiofiles.os.stat(image)
        async with aiofiles.open(image, "r+b") as f:
            resp, maybe_keys = await self.client.upload(
                f,
                content_type=mime_type,  # image/jpeg
                filename=os.path.basename(image),
                filesize=file_stat.st_size)
        if isinstance(resp, UploadResponse):
            self.logger.info("Image was uploaded successfully to server. ")
        else:
            self.logger.warning(f"Failed to upload image. Failure response: {resp}")
            return

        content = {
            "body": os.path.basename(image),  # descriptive title
            "info": {
                "size": file_stat.st_size,
                "mimetype": mime_type,
                "thumbnail_info": None,  # TODO
                "w": width,  # width in pixel
                "h": height,  # height in pixel
                "thumbnail_url": None,  # TODO
            },
            "msgtype": "m.image",
            "url": resp.content_uri,
        }

        try:
            await self.client.room_send(
                room,
                message_type="m.room.message",
                content=content
            )
            self.logger.info("Image was sent successfully")
        except Exception as e:
            self.logger.debug(e)
            self.logger.info(f"Image send of file {image} failed.")

    async def on_message(self, room, event):
        if not self._initial_sync_done:
            return

        if event.sender == self.client.user_id:
            return

        room_id = room.room_id
        msg = event.body

        try:
            await self.client.update_receipt_marker(room_id, event.event_id)
        except Exception as e:
            self.logger.debug("Failed to update receipt marker: %s", e)

        if msg.startswith("!"):
            if msg[1:] == "pic":
                if event.sender in self.users:
                    return
                pic = self._pick_random_picture()
                if pic is None:
                    return
                self.logger.debug("picture for %s: %s", event.sender, event.body)
                if event.sender not in self.bot_owners:
                    self.users.add(event.sender)
                await self.send_image(room_id, str(pic))

            elif msg[1:] == "hello":
                await self.client.room_typing(room_id, True)
                await self.client.room_send(room_id=room_id,
                                            message_type="m.room.message",
                                            content={
                                                "msgtype": "m.text",
                                                "body": "hello"}
                                            )
                await self.client.room_typing(room_id, False)

        return

    async def on_image(self, room_id, event):
        if not self._initial_sync_done:
            return
        self.logger.info(f"Image received in room {room_id}")

    async def on_reaction(self, room, event):
        if not self._initial_sync_done:
            return
        
        room_id = room.room_id
        
        self.logger.debug(f"room_id = {room_id}")
        self.logger.debug(f"event = {event}")

        if isinstance(event, ReactionEvent):
            if event.key == '👍️':
                self.logger.debug("EVENT KEY")
                self.logger.debug(f"User {event.sender} Key {event.source['content']['m.relates_to']['key']}")
                message_event_id = event.source['content']['m.relates_to']['event_id']
                event_id = event.source['event_id']
                self.logger.debug(f"Event ID: {event_id} - Reaction ID {message_event_id}")
                msg = await self.client.room_get_event(room_id=room_id, event_id=message_event_id)

                # self.logger.debug("MSG")
                # self.logger.debug(msg)
                #
                # self.logger.debug("MSG Transport")
                # self.logger.debug(msg.transport_response)

                self.logger.debug("Client Response")

                json_data = await self.client.parse_body(msg.transport_response)

                self.logger.debug("JSON Response")
                self.logger.debug(json_data)

                self.logger.debug("Response Type")
                self.logger.debug(json_data.get('type'))

                if json_data.get('type') == 'm.room.message':
                    sender = event.sender

                    self.logger.debug(sender)

                    if sender not in self.bot_owners:
                        return

                    content = json_data.get('content')

                    self.logger.debug(content.get('msgtype'))

                    if content.get('msgtype') == 'm.image':
                        mxc = content.get('url')
                        server_name = urlparse(mxc).netloc
                        media_id = os.path.basename(urlparse(mxc).path)

                        self.logger.debug(f"MXC = {mxc}")
                        self.logger.debug(f"Server = {server_name}")
                        self.logger.debug(f"Media ID = {media_id}")

                        download = await self.client.download(
                            server_name=server_name,
                            media_id=media_id,
                            filename=None,
                            allow_remote=True,
                        )
                        if not isinstance(download, DownloadResponse):
                            self.logger.warning(
                                "Failed to download reacted-to image: %s", download
                            )
                            return

                        filename = download.filename
                        body = download.body
                        self.logger.debug(f"filename = {filename}")


                        imageStream = io.BytesIO(body)
                        imageFile = Image.open(imageStream)
                        new_hash = str(imagehash.average_hash(imageFile))

                        if self.store.has_hash(new_hash):
                            self.logger.debug("Image found in db")

                            await self.client.room_typing(room_id, True)

                            await self.client.room_send(
                                room_id,
                                message_type="m.room.message",
                                content={
                                    "creator": self.user_id,
                                    "body": "Image already in our database!",
                                    "msgtype": "m.text",
                                    "m.relates_to": {
                                        "m.in_reply_to": {
                                            "event_id": message_event_id
                                        }
                                    }
                                }
                            )

                            await self.client.room_typing(room_id, False)

                            return

                        path = os.path.join(self.path, filename)

                        with open(path, 'wb') as image_file:
                            image_file.write(body)
                        self.store.add_hash(new_hash, filename)

                        event_response = await self.client.room_get_event(room_id, message_event_id)

                        if isinstance(event_response, RoomGetEventError):
                            self.logger.warning(f"Error getting event that was reacted to {message_event_id}")
                            return

                        await self.client.room_typing(room_id, True)

                        await self.client.room_send(
                            room_id,
                            message_type="m.room.message",
                            content={
                                "creator": self.user_id,
                                "body": "Image added to our database! ❤️️",
                                "msgtype": "m.text",
                                "m.relates_to": {
                                    "m.in_reply_to": {
                                        "event_id": message_event_id
                                    }
                                }
                            }
                        )

                        await self.client.room_typing(room_id, False)

                        self.logger.debug("Image download success")

async def main(argv) -> None:

    if len(argv) > 1:
        config_path = argv[1]
    else:
        print("usage: python3 main.py config.yaml")
        sys.exit(1)

    bot = LainBot(config_path)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    sync_task = asyncio.create_task(bot.start(), name="lain-sync")
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        bot.logger.info("Shutdown requested; stopping sync loop")
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        await bot.shutdown()


if __name__ == '__main__':
    asyncio.run(main(sys.argv))
