export interface Thing {
    id: number;
}

export function makeThing(id: number): Thing {
    return { id };
}
