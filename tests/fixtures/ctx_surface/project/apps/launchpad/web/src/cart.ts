import { OrderSchema } from "../../../../lib/launchpad/types/order";
import React from "react";

/** Adds an item to the cart. */
export function addToCart(item: unknown) {
  return OrderSchema.safeParse(item);
}

export const view = React.version;
