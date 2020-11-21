#!/usr/bin/env python3
import asyncio
import os
import re
import sys
import time

import magic
import schedule
import yaml

import aiofiles.os

from PIL import Image

from time import sleep

from random import randint

from nio import AsyncClient, RoomMessageText, MatrixRoom, UploadResponse, RoomMessageImage, SyncError, SyncResponse, \
    InviteMemberEvent

import logging

logging.basicConfig(filename='lain.log', level=logging.DEBUG)

logger = logging.getLogger("LainBot")

class LainBot:

    def __init__(self, config_path):

        logger.info("Initializing system.")

        self.config = None

        with open(config_path, "r") as cfg_file:
            self.config = yaml.safe_load(cfg_file)

        if self.config is None:
            logger.error("Missing config.yaml in run arguments.")
            sys.exit(1)

        self.path = self.config["bot"]["pics_path"]

        logger.info("Start client.")

        self.homeserver = self.config["bot"]["host"]
        self.access_token = self.config["bot"]["token"]
        self.user_id = self.config["bot"]["username"]
        self.device_id = self.config["bot"]["device_name"]

        self.room_id = self.config["bot"]["room_id"]

        self.client = None


        # logger.info("Start job.")
        # schedule.every().day.at("13:37").do(self.job)

        logger.info("Initializing system complete.")

    async def on_error(self, response):
        logger.error(response)
        if self.client:
            await self.client.close()
        sys.exit(1)

    async def on_sync(self, _response):
        if not self._initial_sync_done:
            self._initial_sync_done = True
            for room in self.client.rooms:
                logger.info('room %s', room)
            logger.info('initial sync done, ready for work')

    async def start(self):

        logger.info("Initializing client.")
        self.client = AsyncClient(self.homeserver)
        self.client.access_token = self.access_token
        self.client.user_id = self.user_id
        self.client.device_id = self.device_id

        logger.info("Register callbacks.")

        self.client.add_response_callback(self.on_error, SyncError)
        self.client.add_response_callback(self.on_sync, SyncResponse)
        # self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessageText)
        # self.client.add_event_callback(self.on_image, RoomMessageImage)

        logger.info("Start Sync")
        await self.client.sync_forever(timeout=30000)

    # def job(self):
    #
    #     path = self.path
    #     pic_list = os.listdir(path)
    #     pic_num = randint(a=0, b=len(pic_list) - 1)
    #     pic = pic_list[pic_num]
    #     pic_path = os.path.join(path, pic)
    #
    #     with open(pic_path, 'rb') as pic_file:
    #         pic_bytes = (pic_file.read())
    #
    #     name, extension = pic.split('.')
    #
    #     if extension == "jpg":
    #         extension = "jpeg"
    #
    #     mine = "image/{}".format(extension)
    #     logger.debug(mine)
    #
    #     url = self.client.upload(content=pic_bytes, content_type=mine)
    #
    #     logger.debug(url)
    #
    #     room.send_image(url=url, name=name)
    #
    #     return
    #
    # async def send_image(self, client, room_id, image):
    #     """Send image to toom.
    #     Arguments:
    #     ---------
    #     client : Client
    #     room_id : str
    #     image : str, file name of image
    #     This is a working example for a JPG image.
    #         "content": {
    #             "body": "someimage.jpg",
    #             "info": {
    #                 "size": 5420,
    #                 "mimetype": "image/jpeg",
    #                 "thumbnail_info": {
    #                     "w": 100,
    #                     "h": 100,
    #                     "mimetype": "image/jpeg",
    #                     "size": 2106
    #                 },
    #                 "w": 100,
    #                 "h": 100,
    #                 "thumbnail_url": "mxc://example.com/SomeStrangeThumbnailUriKey"
    #             },
    #             "msgtype": "m.image",
    #             "url": "mxc://example.com/SomeStrangeUriKey"
    #         }
    #     """
    #     mime_type = magic.from_file(image, mime=True)  # e.g. "image/jpeg"
    #     if not mime_type.startswith("image/"):
    #         print("Drop message because file does not have an image mime type.")
    #         return
    #
    #     im = Image.open(image)
    #     (width, height) = im.size  # im.size returns (width,height) tuple
    #
    #     # first do an upload of image, then send URI of upload to room
    #     file_stat = await aiofiles.os.stat(image)
    #     async with aiofiles.open(image, "r+b") as f:
    #         resp, maybe_keys = await client.upload(
    #             f,
    #             content_type=mime_type,  # image/jpeg
    #             filename=os.path.basename(image),
    #             filesize=file_stat.st_size)
    #     if isinstance(resp, UploadResponse):
    #         print("Image was uploaded successfully to server. ")
    #     else:
    #         print(f"Failed to upload image. Failure response: {resp}")
    #
    #     content = {
    #         "body": os.path.basename(image),  # descriptive title
    #         "info": {
    #             "size": file_stat.st_size,
    #             "mimetype": mime_type,
    #             "thumbnail_info": None,  # TODO
    #             "w": width,  # width in pixel
    #             "h": height,  # height in pixel
    #             "thumbnail_url": None,  # TODO
    #         },
    #         "msgtype": "m.image",
    #         "url": resp.content_uri,
    #     }
    #
    #     try:
    #         await client.room_send(
    #             room_id,
    #             message_type="m.room.message",
    #             content=content
    #         )
    #         print("Image was sent successfully")
    #     except Exception:
    #         print(f"Image send of file {image} failed.")

    async def on_message(self, room, event):
        await self.client.update_receipt_marker(room.room_id, event.event_id)
        if event.sender == self.client.user_id:
            return

        # self._last_event_timestamp = event.server_timestamp
        # scripts = self.get_scripts()
        # if not scripts:
        #     logger.error('no scripts')
        #     return
        # for script_path in scripts:
        #     if not re.search(scripts[script_path], event.body, re.IGNORECASE):
        #         continue
        #     logger.debug('trigger %s %s %s %s',
        #         room.room_id,
        #         event.sender,
        #         event.body,
        #         script_path)
        #     await self.client.room_typing(room.room_id, True)
        #     time.sleep(0.4)
        #     await self.client.room_typing(room.room_id, False)
        #     script_output = self.shell_exec(
        #         [script_path, event.body],
        #         {'TMB_ROOM_ID': room.room_id,
        #         'TMB_SENDER': event.sender,
        #         'TMB_BODY': event.body})
        #     for line in script_output.split('\n\n'):
        #         time.sleep(0.8)
        #         logger.debug('send %s %s', room.room_id, line)
        #         await self.client.room_send(
        #             room_id=room.room_id,
        #             message_type='m.room.message',
        #             content={'msgtype': 'm.text', 'body': line})

    # async def image_callback(self, room: MatrixRoom, event: RoomMessageImage) -> None:
    #     logger.debug(f"Image received in room {room.display_name}\n"
    #                    f"{room.user_name(event.sender)} | {event.body}")
    #
    #     logger.debug(f"EVENT: {event}")
    #
    #     if event["sender"] == self.config["bot"]["username"]:
    #         return
    #
    #     owners = self.config["bot"]["owners"]
    #
    #     if event["sender"] not in owners:
    #         return
    #
    #     if event['type'] == "m.room.member":
    #         if event['membership'] == "join":
    #             logger.debug("{0} joined".format(event['content']['displayname']))
    #     elif event['type'] == "m.room.message":
    #         if event['content']['msgtype'] == "m.text":
    #             msg = event['content']['body']
    #             if msg.startswith("!"):
    #                 if msg[1:] == "pic":
    #                     self.job(room)
    #             logger.debug("{0}: {1}".format(event['sender'], event['content']['body']))
    #     elif event['type'] == "m.reaction":
    #         if event['content']['m.relates_to']['key'] == '👍️':
    #             logger.debug(f"User {event['sender']} Key {event['content']['m.relates_to']['key']}")
    #             event_id = event['content']['m.relates_to']['event_id']
    #             logger.debug(f"Event ID: {event_id}")
    #             room_id = event['room_id']
    #
    #             room_state = self.matrix.get_room_state(room_id=room_id)
    #             event_content = room_state
    #
    #             # room_state = self.matrix.room(room_id=room_id, event_type="m.reaction")
    #             # logger.debug(room_state.
    #     else:
    #         logger.debug(event['type'])


async def main(argv):
    config_path = argv[1]

    bot = LainBot(config_path)
    asyncio.run(bot.start())


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main(sys.argv))
