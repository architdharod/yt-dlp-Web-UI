import { describe, expect, it } from "vitest";
import { initials } from "@/components/library/CoverImage";

describe("initials", () => {
  it("takes the first letter of up to two words", () => {
    expect(initials("Black Sands")).toBe("BS");
    expect(initials("Nils Frahm Solo")).toBe("NF");
  });

  it("uses the single letter of a one-word label", () => {
    expect(initials("Bonobo")).toBe("B");
  });

  it("ignores the blanks that surrounding whitespace splits into", () => {
    expect(initials("  Black Sands")).toBe("BS");
    expect(initials("Black  Sands  ")).toBe("BS");
  });

  it("falls back to a question mark when there is no word at all", () => {
    expect(initials("")).toBe("?");
    expect(initials("   ")).toBe("?");
  });
});
