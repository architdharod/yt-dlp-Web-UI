import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Placeholder for the library browser. Phase 4 replaces the body with the
 * artist grid, album grid, and track list served by `GET /library`.
 */
export function LibraryTab() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Library</CardTitle>
        <CardDescription>
          Browsing your library arrives in a later phase.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Downloads land in the library folder as usual; there is nothing to
          browse here yet.
        </p>
      </CardContent>
    </Card>
  );
}
