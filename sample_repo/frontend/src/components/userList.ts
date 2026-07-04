// Renders a list of users. Second cross-file caller of formatUser.

import { fetchUsers } from "../api/usersClient";
import { User, formatUser } from "../models/user";

export async function renderUserList(): Promise<string[]> {
  const users: User[] = await fetchUsers();
  return users.map((u) => formatUser(u));
}
