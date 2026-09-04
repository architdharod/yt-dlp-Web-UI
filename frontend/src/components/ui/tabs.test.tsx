import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

/** The three-tab bar the app renders, at the orientation under test. */
function bar(orientation?: "horizontal" | "vertical") {
  return (
    <Tabs defaultValue="download" orientation={orientation}>
      <TabsList variant="line">
        <TabsTrigger value="download">Download</TabsTrigger>
        <TabsTrigger value="library">Library</TabsTrigger>
      </TabsList>
      <TabsContent value="download">Downloads</TabsContent>
      <TabsContent value="library">Library</TabsContent>
    </Tabs>
  );
}

describe("Tabs", () => {
  it("is horizontal by default, the orientation the styles are written for", () => {
    const { container } = render(bar());

    expect(
      container.querySelector("[data-slot=tabs]")!.getAttribute("data-orientation"),
    ).toBe("horizontal");
    // Base UI leaves aria-orientation off a horizontal tablist, which is the
    // ARIA default.
    expect(
      screen.getByRole("tablist").getAttribute("aria-orientation"),
    ).toBeNull();
  });

  it("forwards orientation to the primitive, which drives aria and focus", () => {
    const { container } = render(bar("vertical"));

    // The wrapper must not swallow the prop: Base UI stamps data-orientation
    // itself and needs the prop for aria-orientation and roving focus.
    expect(
      container.querySelector("[data-slot=tabs]")!.getAttribute("data-orientation"),
    ).toBe("vertical");
    expect(screen.getByRole("tablist").getAttribute("aria-orientation")).toBe(
      "vertical",
    );
  });

  it("shows the default tab's panel", () => {
    render(bar());

    expect(
      screen.getByRole("tab", { name: "Download" }).hasAttribute("data-active"),
    ).toBe(true);
  });
});
