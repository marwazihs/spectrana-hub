/**
 * /r/[id]/consume?token=... — magic-link consume route.
 *
 * Scaffolded in M6.4. Wired in M6.6: will call consumeMagicLink(), set
 * the hub_session cookie, and 302 to /r/[id]. Until then, returns a 501
 * so accidental hits during M6.4 testing are unambiguous.
 */
import { NextResponse } from "next/server";

export async function GET(): Promise<NextResponse> {
  return new NextResponse("Consume route not yet wired (M6.6).", {
    status: 501,
  });
}
