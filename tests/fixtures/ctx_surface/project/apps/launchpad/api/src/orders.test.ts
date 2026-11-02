import { createOrder } from "./orders.ts";

Deno.test("creates an order", () => {
  createOrder({ id: "a" });
});
