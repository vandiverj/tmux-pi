import { greet } from "@/util";
import { fmt, VERSION } from "@/barrel";
import { makeThing, type Thing } from "@lib/thing";
import "missing-package";

const thing: Thing = makeThing(VERSION);
export const message = greet(fmt(thing.id));

export async function later() {
    return import("./format");
}
