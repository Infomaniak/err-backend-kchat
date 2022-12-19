import logging
from errbot.backends.base import RoomOccupant
from .kchatPerson import KchatPerson

log = logging.getLogger("errbot.backends.kchat.roomOccupant")


class KchatRoomOccupant(RoomOccupant, KchatPerson):
    """
    A Person inside a Team (Room)
    """

    def __init__(self, client, teamid, userid, channelid, bot):
        super().__init__(client, userid, channelid)
        self._teamid = teamid
        # Importing inside __init__ to prevent a circular import, which is ugly
        from .kchatRoom import KchatRoom

        self._room = KchatRoom(channelid=channelid, teamid=teamid, bot=bot)

    @property
    def room(self):
        return self._room

    def __unicode__(self):
        return "~{}/{}".format(self._room.name, self.username)

    def __str__(self):
        return self.__unicode__()

    def __eq__(self, other):
        if not isinstance(other, RoomOccupant):
            log.warning(
                "tried to compare a KchatRoomOccupant with"
                f" a KchatPerson {self} vs {other}"
            )
            return False
        return other.room.id == self.room.id and other.userid == self.userid
