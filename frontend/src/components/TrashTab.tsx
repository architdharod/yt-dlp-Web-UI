import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Placeholder for the trash listing. The tab is only mounted when
 * `useTrashCount()` is above zero, which cannot happen until Phase 7 adds
 * `GET /library/trash`, so this is what that phase fills in.
 */
export function TrashTab() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Trash</CardTitle>
        <CardDescription>
          Deleted tracks and albums wait here until the trash is emptied.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Restoring and emptying the trash arrive in a later phase.
        </p>
      </CardContent>
    </Card>
  );
}
