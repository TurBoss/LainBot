#!/usr/bin/env python

import os
import sys
from pprint import pprint

from urllib.parse import urlparse

import magic
import yaml

import time
import asyncio
import aiofiles.os
from asyncio import run
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from PIL import Image

from random import randint

from aiohttp import ClientConnectionError, ServerDisconnectedError

from nio import (AsyncClient,
                 AsyncClientConfig,
                 RoomMessageText,
                 RoomMessage,
                 RoomMessageText,
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
                 LoginResponse,
                 ReactionEvent)

from storage import Storage
from config import Config

import logging
from logging import Formatter

TERM_FORMAT = '[%(name)s][%(levelname)s]  %(message)s (%(filename)s:%(lineno)d)'
FILE_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

class LainBot:
    _initial_sync_done = False

    def __init__(self, config_path):

        self.client_config = None
        self.config = None
        self.loop = asyncio.get_event_loop()

        self.scheduler = AsyncIOScheduler()
        self.config = Config(config_path)
        # with open(config_path, "r") as cfg_file:
        #     self.config = yaml.safe_load(cfg_file)
        #
        # if self.config is None:
        #     sys.exit(13)
        # Configure the database
        self.store = Storage(self.config.database)

        self.logger = logging.getLogger("LainBot")

        # Add console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_format = Formatter(TERM_FORMAT)
        console_handler.setFormatter(console_format)
        self.logger.addHandler(console_handler)


        self.logger.info("Initializing system.")

        self.logger.info("Start client.")

        self.homeserver = self.config.homeserver_url
        self.access_token = self.config.user_token
        self.user_id = self.config.user_id
        self.user_pw = self.config.user_password
        self.bot_owners = self.config.owners
        self.device_id = self.config.device_name
        self.room_id = self.config.room_id
        self.path = self.config.pics_path
        self.event_time = self.config.event_time

        self.client = None
        self.http_client = None
        self.users = list()
        self.hours, self.minutes =  self.event_time.split(':')
        self.scheduler.add_job(self.job, 'cron', day_of_week='mon-sun', hour=self.hours, minute=self.minutes)
        self.scheduler.start()


    async def on_error(self, response):
        self.logger.error(response)
        if self.client:
            self.logger.error("closing client")
            await self.client.close()
        time.sleep(30)

    async def on_sync(self, _response):
        if not self._initial_sync_done:
            self._initial_sync_done = True
            for room in self.client.rooms:
                self.logger.info('room %s', room)
            self.logger.info('initial sync done, ready for work')

    async def start(self):

        self.logger.info("Initializing client.")
        self.client_config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self.client = AsyncClient(self.homeserver, self.config.user_id, device_id=self.config.device_id,
                                  store_path=self.config.store_path, config=self.client_config)
        self.client.access_token = self.access_token
        self.client.user_id = self.user_id
        self.client.device_id = self.device_id

        self.logger.info("Initializing http client.")
        self.http_client = HttpClient(self.homeserver)

        self.logger.info("Register callbacks.")

        self.client.add_response_callback(self.on_error, SyncError)
        self.client.add_response_callback(self.on_sync, SyncResponse)
        # self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessageText)
        self.client.add_event_callback(self.on_reaction, ReactionEvent)
        self.client.add_event_callback(self.on_image, RoomMessageImage)

        self.logger.info("Starting initial sync")
        # Keep trying to reconnect on failure (with some time in-between)
        while True:
            try:

                # Use token to log in
                self.client.load_store()

                # Sync encryption keys with the server
                if self.client.should_upload_keys:
                    await self.client.keys_upload()

                await self.client.sync_forever(timeout=30000)
            except (ClientConnectionError, ServerDisconnectedError):
                self.logger.warning("Unable to connect to homeserver, retrying in 15s...")

                # Sleep so we don't bombard the server with login requests
                time.sleep(15)
            finally:
                # Make sure to close the client connection on disconnect
                await self.client.close()

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

        await self.send_image(self.room_id, pic_path)
        self.logger.info("Job finished")

        self.users.clear()
        return

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
                
        room_id = room.room_id 
        msg = event.body       
        
        await self.client.update_receipt_marker(room_id, event.event_id)
        
        if event.sender == self.client.user_id:
            return

        
        if msg.startswith("!"):
            if msg[1:] == "pic":
                if event.sender in self.users:
                    return
                self.logger.debug("picture for {0}: {1}".format(event.sender, event.body))
                if event.sender not in self.bot_owners:
                    self.users.append(event.sender)
                pic_list = os.listdir(self.path)
                pic_num = randint(a=0, b=len(pic_list) - 1)
                pic = pic_list[pic_num]
                pic_path = os.path.join(self.path, pic)
                await self.send_image(room_id, pic_path)

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
            pprint(event.source)
            if event.source['content']['m.relates_to']['key'] == '❤️':
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

                            await self.client.room_send(
                                room_id,
                                message_type="m.room.message",
                                content={
                                    "creator": self.user_id,                                                            
                                    "body": "Pic added to our database! 👍️",
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
        print("usage: python3 main.py config.yaml")
        sys.exit(1)

    bot = LainBot(config_path)
    
    await bot.start()


if __name__ == '__main__':
    asyncio.run(main(sys.argv))
