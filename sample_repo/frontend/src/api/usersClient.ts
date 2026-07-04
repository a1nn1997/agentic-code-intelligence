// API client for the /users endpoint. Imports formatUser (cross-file edge to
// models/user.ts).

import { User, UserId, formatUser } from "../models/user";

const BASE_URL = "/users";

export async function fetchUsers(): Promise<User[]> {
  const res = await fetch(BASE_URL);
  return (await res.json()) as User[];
}

export async function fetchUser(id: UserId): Promise<User | null> {
  const res = await fetch(`${BASE_URL}/${id}`);
  if (!res.ok) {
    return null;
  }
  return (await res.json()) as User;
}

export function describeUser(user: User): string {
  // First cross-file caller of formatUser.
  return formatUser(user);
}
