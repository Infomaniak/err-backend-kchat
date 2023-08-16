import json
import logging
from functools import lru_cache
from typing import BinaryIO

from errbot.backends.base import (
    Message,
    Presence,
    ONLINE,
    AWAY,
    OFFLINE,
    DND,
    UserDoesNotExistError,
    RoomDoesNotExistError,
    RoomOccupant,
    Card,
    Identifier,
    Stream
)
from errbot.core import ErrBot
from errbot.rendering import md
from errbot.utils import split_string_after
from kchatdriver import Driver
from kchatdriver.exceptions import (
    InvalidOrMissingParameters,
    NotEnoughPermissions,
    ContentTooLarge,
    FeatureDisabled,
    NoAccessTokenProvided,
)

from kchatlib.kchatPerson import KchatPerson
from kchatlib.kchatRoom import KchatRoom
from kchatlib.kchatRoomOccupant import KchatRoomOccupant

log = logging.getLogger("errbot.backends.kchat")

# Default websocket timeout - this is needed to send a heartbeat
# to keep the connection alive
DEFAULT_TIMEOUT = 30

COLORS = {
    "white": "#FFFFFF",
    "cyan": "#00FFFF",
    "blue": "#0000FF",
    "red": "#FF0000",
    "green": "#008000",
    "yellow": "#FFA500",
}


class KchatBackend(ErrBot):
    def __init__(self, config):
        super().__init__(config)
        identity = config.BOT_IDENTITY
        log.setLevel(config.BOT_LOG_LEVEL)

        self._login = identity.get("login", None)
        self._password = identity.get("password", None)
        self._personal_access_token = identity.get("token", None)
        self._mfa_token = identity.get("mfa_token", None)
        self.team = identity.get("team")
        self._scheme = identity.get("scheme", "https")
        self._port = identity.get("port", 443)
        self.cards_hook = identity.get("cards_hook", None)
        self.url = identity.get("server").rstrip("/")
        self.websocket_url = identity.get("websocket_url").rstrip("/")
        self.insecure = identity.get("insecure", False)
        self.timeout = identity.get("timeout", DEFAULT_TIMEOUT)
        self.teamid = ""
        self.token = ""
        self.bot_identifier = None
        self.driver = None
        self.md = md()
        self.event_handlers = {
            "posted": [self._message_event_handler],
            "status_change": [self._status_change_event_handler],
            "pusher_internal:subscription_succeeded": [self._hello_event_handler],
            "user_added": [self._room_joined_event_handler],
            "user_removed": [self._room_left_event_handler],
        }

    def set_message_size_limit(self, limit=16377, hard_limit=16383):
        """
        Kchat message limit is 16383 chars, need to leave some space for
        backticks when messages are split
        """
        super().set_message_size_limit(limit, hard_limit)

    @property
    def userid(self):
        return f"{self.bot_identifier.userid}"

    @property
    def mode(self):
        return "kchat"

    def username_to_userid(self, name):
        """Converts a name prefixed with @ to the userid"""
        name = name.lstrip("@")
        user = self.driver.users.get_user_by_username(username=name)
        if user is None:
            raise UserDoesNotExistError(f"Cannot find user {name}")
        return user["id"]

    def register_handler(self, event, handler):
        if event not in self.event_handlers:
            self.event_handlers[event] = []
        self.event_handlers[event].append(handler)

    async def kchat_event_handler(self, payload):
        if not payload:
            return

        payload = json.loads(payload)
        if "event" not in payload:
            log.debug(f"Message contains no event: {payload}")
            return

        event = payload["event"]
        event_handlers = self.event_handlers.get(event)

        if event_handlers is None:
            log.debug(f"No event handlers available for {event}, ignoring.")
            return
        # noinspection PyBroadException
        for event_handler in event_handlers:
            try:
                event_handler(payload)
            except Exception:
                log.exception(f"{event} event handler raised an exception")

    def _room_joined_event_handler(self, message):
        log.debug("User added to channel")
        if message["user_id"] == self.userid:
            self.callback_room_joined(self)

    def _room_left_event_handler(self, message):
        log.debug("User removed from channel")
        if message["user_id"] == self.userid:
            self.callback_room_left(self)

    def _message_event_handler(self, message):
        log.debug(message)
        data = message

        # In some cases (direct messages) team_id is an empty string
        if data["team_id"] != "" and self.teamid != data["team_id"]:
            log.info(f'Message came from another team ({data["team_id"]}), ignoring...')
            return

        if "channel_id" in data:
            channelid = data["channel_id"]
        else:
            log.error(f"Couldn't find a channelid for event {message}")
            return

        channel_type = data["channel_type"]

        channel = data["channel_name"] if channel_type != "D" else channelid

        text = ""
        post_id = ""
        file_ids = None
        userid = None

        if "post" in data:
            post = data["post"]
            text = post["message"]
            userid = post["user_id"]
            if "file_ids" in post:
                file_ids = post["file_ids"]
            post_id = post["id"]
            if "type" in post and post["type"] == "system_add_remove":
                log.info("Ignoring message from System")
                return

        if "user_id" in data:
            userid = data["user_id"]

        if not userid:
            log.error(f"No userid in event {message}")
            return

        mentions = []
        if "mentions" in data:
            mentions = self.mentions_build_identifier(data["mentions"])

        # Thread root post id
        root_id = post.get("root_id", "")
        parent = self.driver.posts.get_post(root_id) if root_id != "" else None
        msg = Message(
            text,
            parent=parent,
            extras={
                "id": post_id,
                "root_id": root_id,
                "kchat_event": message,
                "url": "{scheme:s}://{domain:s}:{port:s}/{teamname:s}/pl/{postid:s}".format(
                    scheme=self.driver.options["scheme"],
                    domain=self.driver.options["url"],
                    port=str(self.driver.options["port"]),
                    teamname=self.team,
                    postid=post_id,
                ),
            },
        )
        if file_ids:
            msg.extras["attachments"] = file_ids

        # TODO: Slack handles bots here, but I am not sure if bot users is a concept in kchat
        if channel_type == "D":
            msg.frm = KchatPerson(
                self.driver, userid=userid, channelid=channelid, teamid=self.teamid
            )
            msg.to = KchatPerson(
                self.driver,
                userid=self.bot_identifier.userid,
                channelid=channelid,
                teamid=self.teamid,
            )
        else:
            msg.frm = KchatRoomOccupant(
                self.driver,
                userid=userid,
                channelid=channelid,
                teamid=self.teamid,
                bot=self,
            )
            msg.to = KchatRoom(channel, teamid=self.teamid, bot=self)

        self.callback_message(msg)

        if mentions:
            self.callback_mention(msg, mentions)

    def _status_change_event_handler(self, message):
        """Event handler for the 'presence_change' event"""
        idd = KchatPerson(self.driver, message["user_id"])
        status = message["status"]
        if status == "online":
            status = ONLINE
        elif status == "away":
            status = AWAY
        elif status == "offline":
            status = OFFLINE
        elif status == "dnd":
            status = DND
        else:
            log.error(f"It appears the Kchat API changed, I received an unknown status type {status}")
            status = ONLINE
        self.callback_presence(Presence(identifier=idd, status=status))

    def _hello_event_handler(self, message):
        """Event handler for the 'hello' event"""
        self.connect_callback()
        self.callback_presence(Presence(identifier=self.bot_identifier, status=ONLINE))
        self.change_presence()

    @lru_cache(1024)
    def get_direct_channel(self, userid, other_user_id):
        """
        Get the direct channel to another user.
        If it does not exist, it will be created.
        """
        try:
            return self.driver.channels.create_direct_message_channel(options=[userid, other_user_id])
        except (InvalidOrMissingParameters, NotEnoughPermissions):
            raise RoomDoesNotExistError(f"Could not find Direct Channel for users with ID {userid} and {other_user_id}")

    def build_identifier(self, txtrep):
        """
        Convert a textual representation into a
           :class:`~KchatPerson` or :class:`~KchatRoom`

        Supports strings with the following formats::

                @username
                ~channelname
                channelid
        """
        txtrep = txtrep.strip()
        if txtrep.startswith("~"):
            # Channel
            channelid = self.channelname_to_channelid(txtrep[1:])
            if channelid is not None:
                return KchatRoom(channelid=channelid, teamid=self.teamid, bot=self)
        else:
            # Assuming either a channelid or a username
            userid = self.username_to_userid(txtrep[1:]) if txtrep.startswith("@") else txtrep

            if userid is not None:
                return KchatPerson(
                    self.driver,
                    userid=userid,
                    channelid=self.get_direct_channel(self.userid, userid)["id"],
                    teamid=self.teamid,
                )
        raise Exception(f"Invalid or unsupported Kchat identifier: {txtrep}")

    def mentions_build_identifier(self, mentions):
        return [self.build_identifier(mention) for mention in mentions]

    def serve_once(self):
        self.driver = Driver(
            {
                "scheme": self._scheme,
                "url": self.url,
                "websocket_url": self.websocket_url,
                "port": self._port,
                "verify": not self.insecure,
                "timeout": self.timeout,
                "login_id": self._login,
                "password": self._password,
                "token": self._personal_access_token,
                "mfa_token": self._mfa_token,
                "debug": log.getEffectiveLevel() == logging.DEBUG
            }
        )
        self.driver.login()

        self.teamid = self.driver.teams.get_team_by_name(name=self.team)["id"]
        userid = self.driver.users.get_user(user_id="me")["id"]

        self.token = self.driver.client.token

        self.bot_identifier = KchatPerson(
            self.driver, userid=userid, teamid=self.teamid
        )

        # noinspection PyBroadException
        try:
            loop = self.driver.init_websocket(
                event_handler=self.kchat_event_handler,
                team_id=self.teamid,
                team_user_id=self.bot_identifier.userid
            )
            self.reset_reconnection_count()
            loop.run_forever()
        except KeyboardInterrupt:
            log.info("Interrupt received, shutting down..")
            return True
        except Exception:
            log.exception("Error reading from RTM stream:")
        finally:
            log.debug("Triggering disconnect callback")
            self.disconnect_callback()

    def _prepare_message(self, message):
        to_name = "<unknown>"
        if message.is_group:
            to_channel_id = message.to.id
            if message.to.name:
                to_name = message.to.name
            else:
                self.channelid_to_channelname(channelid=to_channel_id)
        else:
            to_name = message.to.username

            if isinstance(
                    message.to, RoomOccupant
            ):  # private to a room occupant -> this is a divert to private !
                log.debug(
                    "This is a divert to private message, sending it directly to the user."
                )
                channel = self.get_direct_channel(
                    self.userid, self.username_to_userid(to_name)
                )
                to_channel_id = channel["id"]
            else:
                to_channel_id = message.to.channelid
        return to_name, to_channel_id

    def send_message(self, message):
        super().send_message(message)
        try:
            to_name, to_channel_id = self._prepare_message(message)

            message_type = "direct" if message.is_direct else "channel"
            log.debug(f"Sending {message_type} message to {to_name} ({to_channel_id})")

            body = self.md.convert(message.body)
            log.debug("Message size: %d" % len(body))

            parts = self.prepare_message_body(body, self.message_size_limit)

            root_id = None
            if message.parent is not None:
                root_id = message.parent.extras.get("root_id")

            for part in parts:
                self.driver.posts.create_post(
                    options={
                        "channel_id": to_channel_id,
                        "message": part,
                        "root_id": root_id,
                    }
                )
        except (InvalidOrMissingParameters, NotEnoughPermissions):
            log.exception(
                "An exception occurred while trying to send the following message "
                "to %s: %s" % (to_name, message.body)
            )

    def _kchat_upload(self, stream: Stream, channel_id: str) -> None:
        """
        Performs an upload defined in a stream
        :param stream: Stream object
        :return: None
        """
        try:
            stream.accept()

            resp = self.driver.files.upload_file(
                channel_id=channel_id, files={'files': (stream.name, stream.raw)}
            )
            stream.file_id = resp.get("file_infos")[0]["id"]
            if resp.get("file_infos"):
                stream.success()
            else:
                stream.error()
        except Exception:
            log.exception(
                f"Upload of {stream.name} to {channel_id} failed."
            )

    def send_stream_request(
            self,
            user: Identifier,
            fsource: BinaryIO,
            name: str = None,
            size: int = None,
            stream_type: str = None,
    ) -> Stream:
        """
        Starts a file transfer. For kChat, the size and stream_type are unsupported
        :param user: is the identifier of the person you want to send it to.
        :param fsource: is a file object you want to send.
        :param name: is an optional filename for it.
        :param size: not supported in kChat backend
        :param stream_type: not supported in kChat backend
        :return Stream: object on which you can monitor the progress of it.
        """
        stream = Stream(user, fsource, name, size, stream_type)
        channel_id = user.channelid if isinstance(user, KchatPerson) else user.id
        log.debug(
            f"Requesting upload of {name} to {channel_id} "
            f"(size hint: {size}, stream type: {stream_type})."
        )
        self._kchat_upload(stream, channel_id)
        return stream

    def send_card(self, card: Card):
        if isinstance(card.to, RoomOccupant):
            card.to = card.to.room

        to_humanreadable, to_channel_id = self._prepare_message(card)

        attachment = {}
        if card.summary:
            attachment["pretext"] = card.summary
        if card.title:
            attachment["title"] = card.title
        if card.link:
            attachment["title_link"] = card.link
        if card.image:
            attachment["image_url"] = card.image
        if card.thumbnail:
            attachment["thumb_url"] = card.thumbnail
        attachment["text"] = card.body

        if card.color:
            attachment["color"] = (
                COLORS[card.color] if card.color in COLORS else card.color
            )

        if card.fields:
            attachment["fields"] = [
                {"title": key, "value": value, "short": True}
                for key, value in card.fields
            ]

        data = {"attachments": [attachment], "channel_id": card.to.channelid}

        try:
            log.debug("Sending data:\n%s", data)
            self.driver.posts.create_post(options=data)
        except (
                InvalidOrMissingParameters,
                NotEnoughPermissions,
                ContentTooLarge,
                FeatureDisabled,
                NoAccessTokenProvided,
        ):
            log.exception(f"An exception occurred while trying to send a card to {to_humanreadable}.[{card}]")

    def prepare_message_body(self, body, size_limit):
        """
        Returns the parts of a message chunked and ready for sending.
        This is a staticmethod for easier testing.
        Args:
                body (str)
                size_limit (int): chunk the body into sizes capped at this maximum
        Returns:
                [str]
        """
        fixed_format = body.startswith("```")  # hack to fix the formatting
        parts = list(split_string_after(body, size_limit))

        if len(parts) == 1:
            # If we've got an open fixed block, close it out
            if parts[0].count("```") % 2 != 0:
                parts[0] += "\n```\n"
        else:
            for i, part in enumerate(parts):
                starts_with_code = part.startswith("```")

                # If we're continuing a fixed block from the last part
                if fixed_format and not starts_with_code:
                    parts[i] = "```\n" + part

                # If we've got an open fixed block, close it out
                if parts[i].count("```") % 2 != 0:
                    parts[i] += "\n```\n"

        return parts

    def change_presence(self, status: str = ONLINE, message: str = ""):
        self.driver.status.update_user_status(
            self.bot_identifier.userid,
            {"user_id": self.bot_identifier.userid, "status": status}
        )

    def is_from_self(self, message: Message):
        return self.bot_identifier.userid == message.frm.userid

    def user_is_typing(self, channelid, parentid=None):
        self.driver.users.user_is_typing(
            self.bot_identifier.userid,
            {"channel_id": channelid, "parent_id": parentid}
        )

    def shutdown(self):
        self.change_presence(status=OFFLINE)
        self.driver.logout()
        super().shutdown()

    def query_room(self, room):
        """Room can either be a name or a channelid"""
        return KchatRoom(room, teamid=self.teamid, bot=self)

    def prefix_groupchat_reply(self, message: Message, identifier):
        super().prefix_groupchat_reply(message, identifier)
        message.body = "@{0}: {1}".format(identifier.nick, message.body)

    def build_reply(self, message, text=None, private=False, threaded=False):
        response = self.build_message(text)
        response.frm = self.bot_identifier
        if private:
            response.to = message.frm
        else:
            response.to = (
                message.frm.room
                if isinstance(message.frm, RoomOccupant)
                else message.frm
            )

        if threaded:
            response.extras["root_id"] = message.extras.get("root_id")
            self.driver.posts.get_post(message.extras.get("root_id"))
            response.parent = message

        return response

    def get_public_channels(self):
        channels = []
        page = 0
        channel_page_limit = 200
        while True:
            channel_list = self.driver.channels.get_public_channels(
                team_id=self.teamid,
                params={"page": page, "per_page": channel_page_limit},
            )
            if len(channel_list) == 0:
                break
            else:
                channels.extend(channel_list)
            page += 1
        return channels

    def channels(self, joined_only=False):
        channels = []
        channels.extend(
            self.driver.channels.get_channels_for_user(
                user_id=self.userid, team_id=self.teamid
            )
        )
        if not joined_only:
            public_channels = self.get_public_channels()
            for channel in public_channels:
                if channel not in channels:
                    channels.append(channel)
        return channels

    def rooms(self):
        """Return public and private channels, but no direct channels"""
        rooms = self.channels(joined_only=True)
        channels = [channel for channel in rooms if channel["type"] != "D"]
        return [
            KchatRoom(channelid=channel["id"], teamid=channel["team_id"], bot=self)
            for channel in channels
        ]

    def channelid_to_channelname(self, channelid):
        """Convert the channelid in the current team to the channel name"""
        channel = self.driver.channels.get_channel(channel_id=channelid)
        if "name" not in channel:
            raise RoomDoesNotExistError(f"No channel with ID {id} exists in team with ID {self.teamid}")
        return channel["name"]

    def channelname_to_channelid(self, name):
        """Convert the channelname in the current team to the channel id"""
        channel = self.driver.channels.get_channel_by_name(
            team_id=self.teamid, channel_name=name
        )
        if "id" not in channel:
            raise RoomDoesNotExistError(f"No channel with name {name} exists in team with ID {self.teamid}")
        return channel["id"]

    def __hash__(self):
        return 0  # This is a singleton anyway
