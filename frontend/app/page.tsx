/**
 * Root path. The viewer surface has no homepage — every user enters
 * through /r/[id] (the magic-link target). Return 404 to discourage
 * crawlers and curious humans alike. Anti-enum parity: no logo, no
 * marketing copy, nothing that confirms what this host is.
 */
import { notFound } from "next/navigation";

export default function Home(): never {
  notFound();
}
