"""持久化冒烟测试：模拟进程重启，验证发件箱与 openid 映射可恢复。"""

import asyncio
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent), str(ROOT)]

PASS = 0
FAIL = 0


def check(name: str, ok: bool) -> None:
    global PASS, FAIL
    PASS += ok
    FAIL += not ok
    print(("  PASS " if ok else "✗ FAIL ") + name)


async def main() -> int:
    from arkbot.persist import Persist
    from arkbot.adapters.official_qq import OfficialQQAdapter

    tmp = Path(tempfile.mkdtemp(prefix="arkbot-persist-"))
    db = tmp / "arkbot.db"

    # ---- 进程 1：入队发件箱 + 学习映射 ----
    p1 = Persist(db)
    p1.save_outbox([
        {"action": "send_group_msg", "params": {"group_id": 1, "message": []}, "at": 0},  # 早已过期
        {"action": "send_private_msg", "params": {"user_id": 2, "message": []}, "at": __import__("time").time()},
    ])
    ob1 = OfficialQQAdapter(app_id="a", app_secret="b")
    ob1.attach_persist(p1)
    ob1._learn_group("GROUP_OPENID_X")
    ob1._learn_user("USER_OPENID_Y")
    p1.close()

    # ---- 进程 2：全新 Persist/适配器实例（模拟重启） ----
    p2 = Persist(db)
    ob2 = OfficialQQAdapter(app_id="a", app_secret="b")
    ob2.attach_persist(p2)

    from arkbot.bot import Bot

    class _App:
        pass

    bot = Bot(app=_App(), adapters=[], persist=p2)
    bot.start_outbox()
    await asyncio.sleep(0.2)
    bot._outbox_task and bot._outbox_task.cancel()
    try:
        await bot._outbox_task
    except asyncio.CancelledError:
        pass

    check("重启后发件箱恢复未过期消息（user_id=2）",
          any(i["params"].get("user_id") == 2 for i in bot._outbox))
    check("重启后过期消息被丢弃（group_id=1 不在）",
          not any(i["params"].get("group_id") == 1 for i in bot._outbox))
    check("重启后群 openid 映射恢复",
          ob2._group_openid_by_id.get(ob1._group_openid_by_id and
                                      next(iter(ob1._group_openid_by_id))) == "GROUP_OPENID_X")
    check("重启后用户 openid 映射恢复",
          "USER_OPENID_Y" in ob2._user_openid_by_id.values())
    check("映射伪 ID 稳定（两进程一致）",
          list(ob2._group_openid_by_id) == list(ob1._group_openid_by_id))
    p2.close()

    # ---- 内存模式降级 ----
    # ---- 内存模式降级（跨平台：父路径是已存在的文件，mkdir 必失败） ----
    blocker = tmp / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    p3 = Persist(blocker / "sub" / "x.db")  # 父路径是文件 → 建目录必失败
    check("非法路径退化为内存模式", p3._db is None and p3.get("k", "d") == "d")

    print(f"\n持久化冒烟: {PASS} 过 / {FAIL} 挂")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
