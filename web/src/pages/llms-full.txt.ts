import type { APIRoute } from "astro";
import * as about from "../content/about.md";
import * as chatgpt from "../content/guides/chatgpt.md";
import * as claude from "../content/guides/claude.md";
import { llms } from "../content/llms";
import * as rules from "../content/rules.md";
import skill from "../content/skill.md?raw";

const body = (doc: { rawContent: () => string; frontmatter: Record<string, string> }) => `# ${doc.frontmatter.title}\n\n${doc.rawContent().trim()}`;
const strip = (md: string) => md.replace(/^---\n[\s\S]*?\n---\n/, "").trim();

export const GET: APIRoute = () =>
  new Response([llms.trim(), body(about), `# Agent skill\n\n${strip(skill)}`, body(claude), body(chatgpt), body(rules)].join("\n\n---\n\n") + "\n", {
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
