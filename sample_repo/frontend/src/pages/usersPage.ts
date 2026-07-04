// Users page. Third cross-file caller of formatUser.

import { fetchUser } from "../api/usersClient";
import { UserId, formatUser } from "../models/user";

export async function usersPage(id: UserId): Promise<string> {
  const user = await fetchUser(id);
  if (user === null) {
    return "not found";
  }
  return formatUser(user);
}
