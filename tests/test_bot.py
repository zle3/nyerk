"""Scheduler tick against a fake Discord channel."""
import asyncio
import itertools

from sqlalchemy import select

from nyerk import bot as botmod
from nyerk import timers
from nyerk.db import get_state
from nyerk.models import Event, SentMessage

from .conftest import ny
from nyerk.games import first as G

_ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, channel, content, view=None):
        self.id = next(_ids)
        self.channel, self.content, self.view = channel, content, view


class FakePartial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def delete(self):
        self.channel.deleted.append(self.id)

    async def edit(self, content=None, view=None, allowed_mentions=None):
        self.channel.edited.append((self.id, content))


class FakeChannel:
    def __init__(self):
        self.sent, self.deleted, self.edited = [], [], []
        self.last_message_id = None

    async def send(self, content, view=None, allowed_mentions=None):
        msg = FakeMessage(self, content, view)
        self.sent.append((msg, allowed_mentions))
        self.last_message_id = msg.id
        return msg

    def get_partial_message(self, mid):
        return FakePartial(self, mid)


def make_bot(monkeypatch, clock):
    channel = FakeChannel()
    b = botmod.NyerkBot(0)

    async def fake_channel(_cid):
        return channel

    monkeypatch.setattr(b, "_channel", fake_channel)
    monkeypatch.setattr(botmod, "now", lambda: clock[0])
    return b, channel


def test_tick_sends_due_alert_and_reposts_dashboard(s, monkeypatch):
    clock = [ny(2026, 10, 9, 13, 25, 1)]
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()

    async def run():
        b, ch = make_bot(monkeypatch, clock)
        await b.tick()
        return ch

    ch = asyncio.run(run())
    contents = [m.content for m, _ in ch.sent]
    assert contents[0].startswith("Dimensional Invasion starts in 5 minutes")
    assert contents[-1].startswith("━")  # dashboard posted last
    assert ch.sent[-1][0].view is not None and len(ch.sent[-1][0].view.children) == 5
    assert s.scalar(select(SentMessage).where(SentMessage.slot == 5)).message_id == ch.sent[0][0].id


def test_tick_after_downtime_skips_and_dedupes(s, monkeypatch):
    clock = [ny(2026, 10, 9, 13, 40)]
    get_state(s).last_tick_at = ny(2026, 10, 9, 13, 0)
    s.commit()

    async def run():
        b, ch = make_bot(monkeypatch, clock)
        await b.tick()
        n = len(ch.sent)
        await b.tick()  # same instant again: nothing new
        return ch, n

    ch, n = asyncio.run(run())
    alerts = [m.content for m, _ in ch.sent if "starts in" in m.content]
    assert alerts == []  # 13:25 warning was missed by more than the grace period
    assert s.scalar(select(SentMessage).where(SentMessage.status == "skipped")) is not None
    assert len(ch.sent) == n


def test_boss_ping_allows_only_the_role(s, monkeypatch):
    from nyerk.models import Category
    s.scalar(select(Category).where(Category.name == "World Boss")).role_id = 777
    boss = s.scalar(select(Event).where(Event.name == "Silent Dartan"))
    start = ny(2026, 10, 9, 12, 0)
    timers.report(s, boss, 1, "Raelin", start)
    s.commit()
    fire = start + 5 * 3600 + 59 * 60  # 6 h respawn, kill assumed 1 minute before the report
    clock = [fire - 15 * 60 + 2]
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()

    async def run():
        b, ch = make_bot(monkeypatch, clock)
        await b.tick()
        return ch

    ch = asyncio.run(run())
    msg, mentions = next((m, a) for m, a in ch.sent if "Silent Dartan spawns" in m.content)
    assert msg.content.startswith("<@&777>\n")
    assert "reported by Raelin" in msg.content
    assert mentions.users is False and mentions.everyone is False
    assert [r.id for r in mentions.roles] == [777]


def test_dashboard_never_deletes_an_alert(s, monkeypatch):
    clock = [ny(2026, 10, 9, 13, 25, 1)]
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()

    async def run():
        b, ch = make_bot(monkeypatch, clock)
        await b.tick()
        alert_id = ch.sent[0][0].id
        G(s).dashboard_message_id = alert_id  # corrupt the stored dashboard ID
        s.commit()
        await b.update_dashboard(1, repost=True)
        return ch, alert_id

    ch, alert_id = asyncio.run(run())
    assert alert_id not in ch.deleted


class _Resp:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, content, view=None, ephemeral=False, allowed_mentions=None):
        self.sent.append((content, view))

    async def edit_message(self, content=None, view=None):
        self.edits.append(content)


def _interaction(uid, name):
    user = type("U", (), {"id": uid, "display_name": name})()
    return type("I", (), {"user": user, "response": _Resp()})()


def test_override_prompt_then_confirm(s, monkeypatch):
    from nyerk import timers as t
    clock = [ny(2026, 10, 9, 12, 0)]
    b, _ = make_bot(monkeypatch, clock)
    first = t.report(s, s.scalar(select(Event).where(Event.name == "Silent Dartan")), 1, "Raelin", clock[0]).timer
    s.commit()

    async def run():
        i = _interaction(2, "Kai")
        await b.handle_boss_report(i, "Silent Dartan")
        text, view = i.response.sent[0]
        assert "Raelin" in text and [c.label for c in view.children] == ["Override their timer", "Keep theirs"]
        c = _interaction(2, "Kai")
        await b.confirm_override(c, view.event_id, view.timer_id, "", "")
        return c.response.edits[0]

    reply = asyncio.run(run())
    assert reply.startswith("Timer set: Silent Dartan")
    s.expire_all()
    assert s.get(type(first), first.id).status == "overridden"


def test_scheduled_alerts_skip_discord_log_channel(s, monkeypatch):
    from nyerk.logbus import bus
    from nyerk.models import LogEntry
    clock = [ny(2026, 10, 9, 13, 25, 1)]  # Dimensional Invasion 5 minute warning
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()

    async def run():
        q = bus.attach_queue()
        b, _ = make_bot(monkeypatch, clock)
        await b.tick()
        lines = []
        while not q.empty():
            lines.append(q.get_nowait())
        return lines

    discord_lines = asyncio.run(run())
    bus.queue = None
    assert not any("Sent Dimensional Invasion" in x for x in discord_lines)
    s.expire_all()
    assert s.scalar(select(LogEntry).where(LogEntry.text.like("Sent Dimensional Invasion%")))  # web Logs page


def test_logs_queue_is_bounded():
    from nyerk import logbus
    bus = logbus.LogBus()

    async def run():
        bus.attach_queue()
        for i in range(logbus.QUEUE_MAX + 50):
            bus.warn("test", f"line {i}")
        return bus.queue.qsize(), bus.take_dropped(), bus.take_dropped()
    assert asyncio.run(run()) == (logbus.QUEUE_MAX, 50, 0)


def test_logs_are_deleted_after_keep_days(s, tmp_path):
    import os
    from nyerk import logbus
    from nyerk.models import LogEntry
    now = 2_000_000_000
    s.add_all([LogEntry(at=now - 20 * 86400, level="INFO", kind="t", text="old"),
               LogEntry(at=now - 3600, level="INFO", kind="t", text="new")])
    s.commit()
    assert logbus.prune_old_rows(14, now) == 1
    assert [r.text for r in s.scalars(select(LogEntry))] == ["new"]
    log = tmp_path / "nyerk.log"
    for name, age in (("nyerk.log.2026-09-01", 20), ("nyerk.log.3", 30), ("nyerk.log.2026-10-10", 1)):
        f = tmp_path / name
        f.write_text("x")
        os.utime(f, (now - age * 86400, now - age * 86400))
    log.write_text("current")
    assert logbus.prune_old_files(log, 14, now) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nyerk.log", "nyerk.log.2026-10-10"]
