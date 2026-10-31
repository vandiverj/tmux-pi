import { z } from "zod";

export const OrderSchema = z.object({ id: z.string() });
export type Order = z.infer<typeof OrderSchema>;
