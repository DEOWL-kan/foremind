"""`foremind controller open|handoff|check|slip`: the interactive controller's sessions (foremind.controller)."""
import sys

from foremind import carriers, seat
from foremind import config as cfg
from foremind import controller as ctl
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    p = sub.add_parser("controller", help="open, hand off and check the interactive controller; record its slips")
    ps = p.add_subparsers(dest="controller_command", required=True, metavar="<subcommand>")
    ps.add_parser("open", help="open a controller session").set_defaults(func=_guard(_open))
    ps.add_parser("handoff", help=f"check {ctl.DOC}, record the handoff, open the successor").set_defaults(
        func=_guard(_handoff))
    ps.add_parser("check", help=f"check the project against {ctl.DOC}'s foremind-check block; all ok: close the "
                  "predecessor").set_defaults(func=_guard(_check))
    s = ps.add_parser("slip", help="record a controller slip with the program's context reading")
    s.add_argument("--kind", required=True, choices=ctl.SLIPS)
    s.add_argument("--note", required=True)
    s.set_defaults(func=_guard(_slip))


def _guard(fn):
    def run(args):
        try:
            return fn(args, find_project_root())
        except (ProjectNotFound, ctl.ControllerError, cfg.ConfigError, seat.SeatError, carriers.CarrierError,
                OSError, ValueError) as e:
            print(f"foremind controller: {e}", file=sys.stderr)
            return 1
    return run


def _carrier(root, config):
    return carriers.get(seat.setting(config, "carrier.kind"), root, config)


def _open(args, root):
    config = cfg.load(root)
    out = ctl.open_controller(root, carrier=_carrier(root, config), config=config)
    print(f"总控会话 {out['session']}（{out['model']} / {out['effort']}）已开出")
    return 0


def _handoff(args, root):
    config = cfg.load(root)
    out = ctl.handoff(root, carrier=_carrier(root, config), config=config)
    print(f"已交接：写交接时上下文 {out['context_tokens']} token（程序读数）；继任 {out['session']}"
          f"（{out['model']} / {out['effort']}）已开出并收到接手指令")
    return 0


def _check(args, root):
    items, ev, closed = ctl.check(root)
    for i in items:
        print(f"{'ok  ' if i['ok'] else '不符'} {i['key']}: {i['want']}" + ("" if i["ok"] else f" → 实际 {i['got']}")
              + (f"（{i['got']}）" if i["ok"] else ""))
    if closed:
        print(f"\n前任 {closed['predecessor']}：" + (
            closed["kept"] if "kept" in closed else f"已关闭（{closed['how']}）" if closed["confirmed"] else
            f"没能确认关闭（{closed.get('error') or '承载层或进程未确认退出'}），请手动关掉它的终端；"
            "再执行 foremind controller check 会重试"))
    print("\n可贴进 PROGRESS.md：\n" + ctl.takeover_line(items, ev))
    return 0 if ev["ok"] else 1


def _slip(args, root):
    ev = ctl.slip(root, args.kind, args.note)
    print(f"已记 controller_slip（{ev['kind']}，会话 {ev['session']}，上下文 {ev['context_tokens']} token）")
    return 0
