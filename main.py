#!/usr/bin/env python3

# A simple chat client for matrix.
# This sample will allow you to connect to a room, and send/recieve messages.
# Args: host:port username password room
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

from random import randint

from matrix_client.client import MatrixClient
from matrix_client.api import MatrixRequestError, MatrixHttpApi
from requests.exceptions import MissingSchema


class LainBot:

    def __init__(self):

        with open("config.yaml", "r") as cfg_file:
            self.config = yaml.load(cfg_file)

        host = self.config["bot"]["host"]
        username = self.config["bot"]["username"]
        password = self.config["bot"]["password"]
        room_id_alias = self.config["bot"]["room"]

        self.path = self.config["bot"]["pics_path"]

        self.api = MatrixHttpApi(base_url=host, token=self.config["bot"]["token"])

        self.client = MatrixClient(host)

        try:
            self.client.login_with_password(username, password)
        except MatrixRequestError as e:
            print(e)
            if e.code == 403:
                print("Bad username or password.")
                sys.exit(4)
            else:
                print("Check your sever details are correct.")
                sys.exit(2)
        except MissingSchema as e:
            print("Bad URL format.")
            print(e)
            sys.exit(3)

        try:
            room = self.client.join_room(room_id_alias)
        except MatrixRequestError as e:
            print(e)
            if e.code == 400:
                print("Room ID/Alias in the wrong format")
                sys.exit(11)
            else:
                print("Couldn't find room.")
                sys.exit(12)

        room.add_listener(self.on_message)
        self.client.start_listener_thread()

        self.room_id = self.api.get_room_id(self.config["bot"]["room"])

        schedule.every().day.at("17:10").do(self.job, room=room)

    @staticmethod
    def start():
        while 1:
            schedule.run_pending()

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
        print(mine)

        url = self.client.upload(content=pic_bytes, content_type=mine)

        print(url)

        room.send_image(url=url, name=name)

        return

    # Called when a message is recieved.
    def on_message(self, room, event):

        if event["sender"] == self.config["bot"]["username"]:
            return

        power_levels = self.api.get_power_levels(room_id=self.room_id)

        for key, value in power_levels.items():
            if key == "users" and event["sender"] not in value:
                return

        if event['type'] == "m.room.member":
            if event['membership'] == "join":
                print("{0} joined".format(event['content']['displayname']))
        elif event['type'] == "m.room.message":
            if event['content']['msgtype'] == "m.text":
                msg = event['content']['body']
                if msg.startswith("!"):
                    if msg[1:] == "pic":
                        self.job(room)
                print("{0}: {1}".format(event['sender'], event['content']['body']))

        else:
            print(event['type'])


def main():
    bot = LainBot()
    bot.start()


if __name__ == '__main__':
    main()
