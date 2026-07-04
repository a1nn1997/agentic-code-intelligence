// Tests for the TS users client. Call sites here also count as references to
// formatUser and fetchUsers.

import { fetchUsers } from "../src/api/usersClient";
import { User, formatUser } from "../src/models/user";

describe("formatUser", () => {
  it("formats an active user", () => {
    const user: User = {
      id: "u1",
      name: "Ada Lovelace",
      email: "ada@example.com",
      active: true,
    };
    expect(formatUser(user)).toContain("active");
  });
});

describe("fetchUsers", () => {
  it("is callable", () => {
    expect(typeof fetchUsers).toBe("function");
  });
});
