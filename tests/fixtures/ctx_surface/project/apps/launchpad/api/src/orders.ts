import { z } from "zod";
import { OrderSchema } from "../../../../lib/launchpad/types/order.ts";
import { slug } from "./util.ts";

export function createOrder(input: unknown) {
  return OrderSchema.parse(input);
}

export function cancelOrder(id: string) {
  return slug(id);
}

export const OrderStatus = z.enum(["open", "closed"]);
