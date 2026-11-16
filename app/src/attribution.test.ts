import { expect, test } from "bun:test";
import { attributionOf, readAttribution, withAttribution } from "./attribution";

test("only short slugs and agent addresses are taken from the address", () => {
  expect(readAttribution(`?utm_source=x&utm_campaign=kickoff-clash-2&ref=0x${"a".repeat(40)}&other=1`)).toEqual({
    utm_source: "x",
    utm_campaign: "kickoff-clash-2",
    ref: `0x${"a".repeat(40)}`,
  });
  expect(readAttribution("?utm_source=<script>&ref=has%20space")).toBeUndefined();
  expect(readAttribution("")).toBeUndefined();
});

test("the tags ride the OAuth state after the nonce and come back intact", () => {
  const state = withAttribution("abc123", { utm_source: "reddit", ref: "r_polymarket" });
  expect(state.startsWith("abc123.")).toBe(true);
  expect(attributionOf(state)).toEqual({ utm_source: "reddit", ref: "r_polymarket" });
  expect(withAttribution("abc123", undefined)).toBe("abc123");
});

test("a state with no tags, or tampered ones, carries nothing", () => {
  expect([attributionOf("abc123"), attributionOf(null), attributionOf("abc.%%%"), attributionOf(`abc.${btoa('{"ref":"<x>"}')}`)]).toEqual([undefined, undefined, undefined, undefined]);
});
