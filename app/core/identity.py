"""Who is speaking: one Person for voice and Telegram, from photolib.

photolib is the source of truth for people: its users (app_user) carry the
person, the admin flag and the permissions (sections of its API), its tree
the person's name. A voice profile is linked to a person in voices.PERSON.
"""
from dataclasses import dataclass, field

from core import profile
import photolib
import voices
from core.util import log


@dataclass
class Person:
    name: str | None                 # how Eva calls them; None = not recognised
    person_id: int | None = None     # photolib person
    profile: str | None = None       # voice profile / memory speaker label
    is_admin: bool = False
    permissions: set = field(default_factory=set)   # photolib sections
    channel: str = "voice"           # voice | telegram
    telegram_id: str | None = None   # the account writing, when channel is telegram
    guessed: bool = False            # by the nearest voice only (a short phrase left unsigned): no private, no control

    @property
    def known(self):
        return self.name is not None

    def _remote_admin(self):
        """Is this Telegram account an admin in photolib - asked of photolib now
        (its user list, cached a few minutes), not taken from a list here."""
        if self.channel != "telegram" or not self.telegram_id:
            return False
        try:
            u = photolib.user_by_telegram(self.telegram_id)
        except Exception as e:
            log("identity: photolib:", e)
            return False
        return bool(u and u.get("is_admin"))

    def may(self, need):
        """Access check for a tool's `needs` (docs/ARCHITECTURE.md, Access policy)."""
        if need is None:
            return True
        if self.guessed and need in ("private", "control", "admin"):
            return False                  # a guess is enough to talk, not for health, money or the house
        if need == "screen":
            return self.channel == "voice"     # the TV is in the room: anybody there may say «дальше»
        if not self.known:
            return False                  # an unknown voice gets no personal data
        if need in REMOTE_LOCKED and self.channel != "voice":
            # a stolen account is the threat; photolib admins' accounts are the exception for "private"
            if not (need in REMOTE_ADMIN_OK and self._remote_admin()):
                return False
        if need == "known" or self.is_admin:
            return True
        if need == "admin":
            return False
        if need == "adult":
            return self.name not in ADULTS_EXCLUDED
        return need in self.permissions


# children: no finances, no acting in the house (BOOTSTRAP "Доступ")
ADULTS_EXCLUDED = profile.CHILDREN          # eva/profile.json
# Only at home, by voice (Иван, 2026-11-19): in Telegram a stolen account or
# phone is the real threat, so until a second factor exists there is
#   "private" - health, documents, money (eva/PRIVATE.md),
#   "control" - acting in the house: power switching (stove, sockets...), settings.
# Skills mark such tools with needs="control" / "private".
REMOTE_LOCKED = {"private", "control"}
# ...except: "private" (money, documents) in Telegram for the accounts that are
# admins in photolib (user, 2026-11-23). "control" stays voice-only for everybody.
REMOTE_ADMIN_OK = set(profile.tune("identity.remote_admin_ok", ["private"]))
GUEST = Person(None)


def _from_users(person_id, name, profile, channel, telegram_id=None):
    perms, admin = set(), False
    try:
        for u in photolib.users():
            if u.get("person_id") == person_id:
                admin = admin or bool(u.get("is_admin"))
                perms |= set(u.get("permissions") or [])
    except Exception as e:  # photolib away: name only, the most basic rights
        log("identity: photolib:", e)
    return Person(name, person_id, profile, admin, perms, channel,
                  str(telegram_id) if telegram_id else None)


def from_voice(label, known):
    """A recognised voice profile -> Person (photolib person if linked)."""
    if not known:
        return GUEST
    name = voices.CALL.get(label, label)
    pid = voices.PERSON.get(label)
    if pid is None:
        return Person(name, None, label, channel="voice")
    return _from_users(pid, name, label, "voice")


def from_telegram(user):
    """A photolib user (by Telegram id) -> Person."""
    pid = user.get("person_id")
    profile = next((k for k, v in voices.PERSON.items() if v == pid), None) or user.get("person_name")
    name = user.get("person_name") or user.get("display_name")
    return _from_users(pid, name, profile, "telegram", user.get("telegram_id"))
