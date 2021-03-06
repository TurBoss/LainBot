#!/usr/bin/env python3

import os
import re
import sys

import magic
import time
import yaml

import asyncio
import aiofiles.os
import aioschedule as schedule

from PIL import Image

from random import randint

from nio import (AsyncClient,
                 RoomMessageText,
                 MatrixRoom,
                 UploadResponse,
                 RoomMessageImage,
                 SyncError,
                 SyncResponse,
                 InviteMemberEvent,
                 Event,
                 UnknownEvent)

import logging


class LainBot:
    _initial_sync_done = False

    def __init__(self, config_path):

        self.config = None
        self.loop = asyncio.get_event_loop()

        self.scheduler = schedule.default_scheduler

        with open(config_path, "r") as cfg_file:
            self.config = yaml.safe_load(cfg_file)

        if self.config is None:
            sys.exit(13)

        log_file = self.config["bot"]["log_file"]
        logging.basicConfig(filename=log_file, level=logging.DEBUG)

        self.logger = logging.getLogger("LainBot")
        self.logger.info("Initializing system.")

        self.logger.info("Start client.")

        self.homeserver = self.config["bot"]["host"]
        self.access_token = self.config["bot"]["token"]
        self.user_id = self.config["bot"]["username"]
        self.bot_owners = self.config["bot"]["owners"]
        self.device_id = self.config["bot"]["device_name"]
        self.room_id = self.config["bot"]["room_id"]
        self.path = self.config["bot"]["pics_path"]
        self.event_time = self.config["bot"]["event_time"]

        self.client = None
        self.users = list()

        self.logger.info("Register job.")
        self.scheduler.every().day.at(self.event_time).do(self.job)
        self.loop.create_task(self.timer())

        self.logger.info("Initializing system complete.")

    async def on_error(self, response):
        self.logger.error(response)
        if self.client:
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

        self.logger.info("Register callbacks.")
        self.client.add_response_callback(self.on_error, SyncError)
        self.client.add_response_callback(self.on_sync, SyncResponse)
        # self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessageText)
        self.client.add_event_callback(self.on_unknown, UnknownEvent)
        self.client.add_event_callback(self.on_image, RoomMessageImage)

        self.logger.info("Starting initial sync")

        await self.client.sync_forever(timeout=30000)

    async def timer(self):
        # Timer function that runs pending jobs in scheduler,
        # Is meant to be run in clients event loop by calling
        # client.loop.create_task(self.timer())
        while True:
            await self.scheduler.run_pending()
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

    async def on_message(self, room, event):
        await self.client.update_receipt_marker(room.room_id, event.event_id)

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
                await self.client.room_typing(room.room_id, True)
                await self.client.room_send(self.room_id,
                                            message_type="m.room.message",
                                            content="hello")
                await self.client.room_typing(room.room_id, False)

        return

    async def on_image(self, room, event):
        if not self._initial_sync_done:
            return
        self.logger.debug(f"Image received in room {room.display_name}\n"
                     "{room.user_name(event.sender)} | {event.body}")

    def on_unknown(self, room, event):
        if not self._initial_sync_done:
            return

        self.logger.debug(room.room_id)
        self.logger.debug(event)

        # if event['type'] == "m.reaction":
        #     if event['content']['m.relates_to']['key'] == '👍️':
        #         logger.debug(f"User {event['sender']} Key {event['content']['m.relates_to']['key']}")
        #         event_id = event['content']['m.relates_to']['event_id']
        #         logger.debug(f"Event ID: {event_id}")
        #         room_id = event['room_id']
        #
        #         logger.debug("EVENT KEY")


async def main(argv):
    config_path = argv[1]

    bot = LainBot(config_path)
    await bot.start()


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main(sys.argv))
