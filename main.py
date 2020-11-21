#!/usr/bin/env python3


# Error Codes:
# 1 - Unknown problem has occured
# 2 - Could not find the server.
# 3 - Bad URL Format.
# 4 - Bad username/password.
# 11 - Wrong room format.
# 12 - Couldn't find room.

import os
import sys
import schedule
import yaml

from time import sleep

from random import randint

from matrix_client.client import MatrixClient
from matrix_client.api import MatrixRequestError, MatrixHttpApi
from requests.exceptions import MissingSchema

import logging

logging.basicConfig(filename='lain.log', level=logging.DEBUG)


class LainBot:

    def __init__(self, config_path):

        self.log = logging.getLogger(__name__)
        self.log.info("Initializing system.")

        self.config = None

        with open(config_path, "r") as cfg_file:
            self.config = yaml.safe_load(cfg_file)

        if self.config is None:
            sys.exit(13)

        host = self.config["bot"]["host"]
        username = self.config["bot"]["username"]
        password = self.config["bot"]["password"]
        room_id_alias = self.config["bot"]["room"]

        self.path = self.config["bot"]["pics_path"]

        self.log.info("Start client.")
        self.client = MatrixClient(host)
        self.matrix = MatrixHttpApi("https://jauriarts.org", token="some_token")

        # response = matrix.send_message("!roomid:matrix.org", "Hello!")

        try:
            self.log.info("Client login.")
            self.client.login(username=username, password=password, sync=True)
        except MatrixRequestError as e:
            self.log.debug(e)
            if e.code == 403:
                self.log.info("Bad username or password.")
                sys.exit(4)
            else:
                self.log.info("Check your sever details are correct.")
                sys.exit(2)
        except MissingSchema as e:
            self.log.debug("Bad URL format.")
            self.log.debug(e)
            sys.exit(3)

        try:
            room = self.client.join_room(room_id_alias)
        except MatrixRequestError as e:
            self.log.debug(e)
            if e.code == 400:
                self.log.info("Room ID/Alias in the wrong format")
                sys.exit(11)
            else:
                self.log.info("Couldn't find room.")
                sys.exit(12)

        self.log.info("Start listener.")
        room.add_listener(self.on_message)
        self.client.start_listener_thread()

        self.room_id = self.config["bot"]["room_id"]

        self.log.info("Start job.")
        schedule.every().day.at("13:37").do(self.job, room=room)

        self.log.info("Initializing system complete.")

    @staticmethod
    def start():
        while True:
            schedule.run_pending()
            sleep(1)

    def job(self, room):

        path = self.path

        pic_list = os.listdir(path)

        pic_num = randint(a=0, b=len(pic_list) - 1)

        pic = pic_list[pic_num]

        pic_path = os.path.join(path, pic)

        with open(pic_path, 'rb') as pic_file:
            pic_bytes = (pic_file.read())

        name, extension = pic.split('.')

        if extension == "jpg":
            extension = "jpeg"

        mine = "image/{}".format(extension)
        self.log.debug(mine)

        url = self.client.upload(content=pic_bytes, content_type=mine)

        self.log.debug(url)

        room.send_image(url=url, name=name)

        return

    # Called when a message is recieved.
    def on_message(self, room, event):
        self.log.debug(f"EVENT: {event}")

        if event["sender"] == self.config["bot"]["username"]:
            return

        owners = self.config["bot"]["owners"]

        if event["sender"] not in owners:
            return

        if event['type'] == "m.room.member":
            if event['membership'] == "join":
                self.log.debug("{0} joined".format(event['content']['displayname']))
        elif event['type'] == "m.room.message":
            if event['content']['msgtype'] == "m.text":
                msg = event['content']['body']
                if msg.startswith("!"):
                    if msg[1:] == "pic":
                        self.job(room)
                self.log.debug("{0}: {1}".format(event['sender'], event['content']['body']))
        elif event['type'] == "m.reaction":
            if event['content']['m.relates_to']['key'] == '👍️':
                self.log.debug(f"User {event['sender']} Key {event['content']['m.relates_to']['key']}")
                event_id = event['content']['m.relates_to']['event_id']
                self.log.debug(f"Event ID: {event_id}")


        else:
            self.log.debug(event['type'])


def main(argv):

    config_path = argv[1]

    bot = LainBot(config_path)
    bot.start()


if __name__ == '__main__':
    main(sys.argv)
