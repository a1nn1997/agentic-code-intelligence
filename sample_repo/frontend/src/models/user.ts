// User domain models for the TypeScript client.
// `formatUser` is the cross-file / rename symbol on the TS side: defined here,
// called from usersClient.ts, components/userList.ts, pages/usersPage.ts, tests.

export type UserId = string;

export interface User {
  id: UserId;
  name: string;
  email: string;
  active: boolean;
}

export function formatUser(user: User): string {
  const status = user.active ? "active" : "inactive";
  return `${user.name} <${user.email}> (${status})`;
}
