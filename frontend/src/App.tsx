import { Badge } from "@/components/ui/badge";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { DownloadTab } from "@/components/DownloadTab";
import { LibraryTab } from "@/components/LibraryTab";
import { TrashTab } from "@/components/TrashTab";
import { useActiveJobCount } from "@/hooks/useQueueQuery";
import { useQueueStream } from "@/hooks/useQueueStream";
import { useTrashCount } from "@/hooks/useTrashCount";

/**
 * The whole app: a tab bar over the three views, with the queue's SSE stream
 * held open above them so events keep patching the cache whichever tab is on
 * screen.
 */
export function App() {
  const { error } = useQueueStream();
  const activeJobs = useActiveJobCount();
  const trashCount = useTrashCount();

  return (
    <div className="mx-auto flex h-dvh max-w-2xl flex-col gap-4 overflow-hidden p-4 sm:p-6">
      <Tabs
        defaultValue="download"
        className="flex min-h-0 flex-1 flex-col gap-4"
      >
        <TabsList variant="line" className="w-full justify-start border-b">
          <TabsTrigger value="download" className="flex-none">
            Download
            {activeJobs > 0 && (
              <Badge variant="secondary" className="tabular-nums">
                {activeJobs}
              </Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="library" className="flex-none">
            Library
          </TabsTrigger>
          {/* The Trash tab does not exist while the trash is empty. */}
          {trashCount > 0 && (
            <TabsTrigger value="trash" className="flex-none">
              Trash
              <Badge variant="destructive" className="tabular-nums">
                {trashCount}
              </Badge>
            </TabsTrigger>
          )}
        </TabsList>

        {/*
          Only the stream's error is shown. `connected` is false on every first
          render before the stream opens, so a notice keyed off it flashed on
          load, and during a real outage it said the same thing as the error.
        */}
        {error && <p className="shrink-0 text-xs text-destructive">{error}</p>}

        {/*
          keepMounted so a half-typed URL survives a look at another tab.
          Base UI hides a mounted-but-inactive panel with the `hidden`
          attribute, which a `flex` utility would otherwise override, hence the
          attribute selector.
        */}
        <TabsContent
          value="download"
          keepMounted
          className="flex min-h-0 flex-1 flex-col [&[hidden]]:hidden"
        >
          <DownloadTab />
        </TabsContent>
        <TabsContent value="library" className="min-h-0 flex-1 overflow-y-auto">
          <LibraryTab />
        </TabsContent>
        {trashCount > 0 && (
          <TabsContent value="trash" className="min-h-0 flex-1 overflow-y-auto">
            <TrashTab />
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}
