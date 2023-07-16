#!/usr/bin/env python

import os
import sys

from urllib.parse import urlparse

import magic
import yaml

import asyncio
import aiofiles.os
from asyncio import run
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from PIL import Image

from random import randint

from nio import (AsyncClient,
                 RoomMessageText,
                 RoomMessage,
                 MatrixRoom,
                 UploadResponse,
                 RoomMessageImage,
                 RoomGetEventError,
                 SyncError,
                 SyncResponse,
                 InviteMemberEvent,
                 Event,
                 UnknownEvent,
                 HttpClient,
                 DownloadResponse,
                 LoginResponse)

import logging


class LainBot:
    _initial_sync_done = False

    def __init__(self, config_path):

        self.config = None
        self.loop = asyncio.get_event_loop()

        self.scheduler = AsyncIOScheduler()

        with open(config_path, "r") as cfg_file:
            self.config = yaml.safe_load(cfg_file)

        if self.config is None:
            sys.exit(13)

        log_file = self.config["bot"]["log_file"]
        formatter = "%(asctime)s; %(levelname)s; %(message)s"
        logging.basicConfig(filename=log_file, level=logging.DEBUG, format=formatter)

        self.logger = logging.getLogger("LainBot")
        self.logger.info("Initializing system.")

        self.logger.info("Start client.")

        self.homeserver = self.config["bot"]["host"]
        self.access_token = self.config["bot"]["token"]
        self.user_id = self.config["bot"]["username"]
        self.user_pw = self.config["bot"]["password"]
        self.bot_owners = self.config["bot"]["owners"]
        self.device_id = self.config["bot"]["device_name"]
        self.room_id = self.config["bot"]["room_id"]
        self.path = self.config["bot"]["pics_path"]
        self.event_time = self.config["bot"]["event_time"]

        self.client = None
        self.http_client = None
        self.users = list()

        self.scheduler.add_job(self.job, 'cron', day_of_week='mon-sun', hour=13, minute=37)
        self.scheduler.start()



    async def on_error(self, response):
        self.logger.error(response)
        if self.client:
            self.logger.error("closing client")
            await self.client.close()
        sys.exit(1)

    async def on_sync(self, _response):
        if not self._initial_sync_done:
            self._initial_sync_done = True
            for room in self.client.rooms:
                self.logger.info('room %s', room)
            self.logger.info('initial sync done, ready for work')

    async def start(self):

        self.logger.info("Initializing client.")

        self.client = AsyncClient(self.homeserver)
        self.client.access_token = self.access_token
        self.client.user_id = self.user_id
        self.client.device_id = self.device_id

        self.logger.info("Initializing http client.")
        self.http_client = HttpClient(self.homeserver)

        self.logger.info("Register callbacks.")

        self.client.add_response_callback(self.on_error, SyncError)
        self.client.add_response_callback(self.on_sync, SyncResponse)
        # self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessage)
        self.client.add_event_callback(self.on_unknown, UnknownEvent)
        self.client.add_event_callback(self.on_image, RoomMessageImage)

        self.logger.info("Starting initial sync")

        await self.client.sync_forever(timeout=30000)

    async def timer(self):
        # Timer function that runs pending jobs in scheduler,
        # Is meant to be run in clients event loop by calling
        # client.loop.create_task(self.timer())
        while True:
            self.scheduler.run_pending()
            await asyncio.sleep(1)

    async def job(self):
        self.logger.info("Job started")
        pic_list = os.listdir(self.path)
        pic_num = randint(a=0, b=len(pic_list) - 1)
        pic = pic_list[pic_num]

        self.logger.info(f"Upload {pic}")

        pic_path = os.path.join(self.path, pic)

        await self.send_image(pic_path)
        self.logger.info("Job finished")

        self.users.clear()
        return

    async def send_image(self, image):
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
        mime_type = magic.from_file(image, mime=True)  # e.g. "image/jpeg"
        if not mime_type.startswith("image/"):
            self.logger.info("Drop message because file does not have an image mime type.")
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
            self.logger.info(f"Failed to upload image. Failure response: {resp}")

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
                self.room_id,
                message_type="m.room.message",
                content=content
            )
            self.logger.info("Image was sent successfully")
        except Exception as e:
            self.logger.debug(e)
            self.logger.info(f"Image send of file {image} failed.")

    async def on_message(self, room_id, event):
        await self.client.update_receipt_marker(room_id, event.event_id)

        if not self._initial_sync_done:
            return
        if event.sender == self.client.user_id:
            return

        msg = event.body
        if msg.startswith("!"):
            if msg[1:] == "pic":
                if event.sender in self.users:
                    return
                self.logger.debug("picture for {0}: {1}".format(event.sender, event.body))
                self.users.append(event.sender)
                pic_list = os.listdir(self.path)
                pic_num = randint(a=0, b=len(pic_list) - 1)
                pic = pic_list[pic_num]
                pic_path = os.path.join(self.path, pic)
                await self.send_image(pic_path)

            elif msg[1:] == "hello":
                await self.client.room_typing(room_id, True)
                await self.client.room_send(room_id,
                                            message_type="m.room.message",
                                            content={"body": "hello",
                                                     "msgtype": "m.text"})
                await self.client.room_typing(room_id, False)

        return

    async def on_image(self, room_id, event):
        if not self._initial_sync_done:
            return
        self.logger.debug(f"Image received in room {room_id}")

    async def on_unknown(self, room_id, event):
        if not self._initial_sync_done:
            return

        self.logger.debug(f"room_id = {room_id}")
        self.logger.debug(f"event = {event}")

        if event.type == "m.reaction":
            if event.source['content']['m.relates_to']['key'] == '👍️':
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

                        try:

                            image = await self.client.download(server_name=server_name, media_id=media_id, filename=None, allow_remote=True)
                            assert isinstance(image, DownloadResponse)

                            filename = image.filename
                            body = image.body
                            self.logger.debug(f"filename = {filename}")

                            path = os.path.join(self.path, filename)

                            with open(path, 'wb') as image_file:
                                image_file.write(body)
                                image_file.close()
                            event_response = await self.client.room_get_event(room_id, message_event_id)

                            if isinstance(event_response, RoomGetEventError):
                                self.logger.warning(f"Error getting event that was reacted to {message_event_id}")
                                return

                            # await self.client.room_typing(room_id, True)

                            await self.client.room_send(room_id,
                                                        message_type="m.room.message",
                                                        content={"body": "Image added to my collection! 👍️",
                                                                 "msgtype": "m.text",
                                                                 "m.relates_to": {
                                                                     "m.in_reply_to": {
                                                                         "event_id": message_event_id
                                                                         }
                                                                     }
                                                                 }
                                                        )
                            # await self.client.room_typing(room_id, False)

                            self.logger.debug("Image download success")

                        except Exception as e:
                            self.logger.error(e)

                # message_content = json_data.get("content")
                # self.logger.debug(message_content.type)
                #
                # if message_content.type == 'm.room.message':
                #     self.logger.debug("GOT Image")
                #     self.logger.debug(message_content.url)


async def main(argv) -> None:

    if len(argv) > 1:
        config_path = argv[1]
    else:
        print("usage: config.yaml")
        sys.exit(1)

    bot = LainBot(config_path)
    
    await bot.start()


if __name__ == '__main__':
    asyncio.run(main(sys.argv))
