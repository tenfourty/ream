# Personal machines (agents design, slice 4)

A key's agent is shared by default. The owner can mark it as one person's machine: it then reads
what that person reads (for the owner, every live page, pages closed to agents included, never the
trash) and still changes only its own pages. Nothing changes until a machine is marked.

Mark: the Machines page (`/machines`, the owner only): "Belongs to", then Save. Or on the pod:
`artifacts-admin set-agent-person <agent> <person-id>`. Undo: choose "Shared" on the page, or
`artifacts-admin clear-agent-person <agent>`. Both take effect on the key's next request; the key
need not be rotated. The agent must hold a key and the person must exist (`person` table); an app
agent (connected through `/mcp` sign-in) already belongs to its person and is refused.

What follows: the agent's questions go to its person; `GET /api/v1/me` and MCP `whoami` add
`belongs_to` and `shown_as`; a rename will not merge it with an agent of another person
(`refused: ... different people`); `change-owner` moves the owner's marked machines to the new id.

Rollback: an older image ignores `agent_person`, so a personal key narrows back to a key's reach.
