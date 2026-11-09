"""Command line: `jobq run` starts a worker, `jobq enqueue NAME JSON` queues one job in this process only."""
import argparse
import json
import sys

from jobq import Job, JobQueue
from settings import load_settings
from worker import HANDLERS, run_forever


def build_parser():
    parser = argparse.ArgumentParser(prog="jobq")
    parser.add_argument("--config", help="path to a settings JSON file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run jobs until interrupted")
    enqueue = sub.add_parser("enqueue", help="add one job")
    enqueue.add_argument("name")
    enqueue.add_argument("payload", help="JSON object")
    sub.add_parser("handlers", help="list registered handler names")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.command == "handlers":
        print("\n".join(sorted(HANDLERS)))
        return 0
    queue = JobQueue()
    if args.command == "enqueue":
        queue.push(Job(args.name, json.loads(args.payload)))
        print("queued", args.name)
        return 0
    run_forever(queue, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
