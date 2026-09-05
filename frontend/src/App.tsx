import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { DownloadTab } from "@/components/DownloadTab";
import { LibraryTab } from "@/components/LibraryTab";
import { NoticeBanner } from "@/components/NoticeBanner";
import { TrashTab } from "@/components/TrashTab";
import { useActiveJobCount } from "@/hooks/useQueueQuery";
import { useQueueStream } from "@/hooks/useQueueStream";
import { useTrashCount } from "@/hooks/useTrashQuery";

/**
 * The whole app: a tab bar over the three views, with the queue's SSE stream
 * held open above them so events keep patching the cache whichever tab is on
 * screen.
 */
export function App() {
  const { error } = useQueueStream();
  const activeJobs = useActiveJobCount();
  const trashCount = useTrashCount();
  const [tab, setTab] = useState("download");

  // The Trash tab stops existing the moment the last entry is restored or the
  // trash is emptied, and a `value` naming a tab that is no longer in the list
  // would leave every panel hidden. The state is reconciled during render
  // rather than in an effect, so the fallback sticks: an effect would leave
  // `tab` saying "trash", and refilling the trash would snap the user back
  // onto a tab they never chose. The derivation below stays as insurance
  // against the frame between the `setTab` and the re-render.
  if (tab === "trash" && trashCount === 0) setTab("library");
  const activeTab = tab === "trash" && trashCount === 0 ? "library" : tab;

  return (
    <div className="mx-auto flex h-dvh max-w-2xl flex-col gap-4 overflow-hidden p-4 sm:p-6">
      {/*
        Above the tabs, not inside one: a Navidrome or Lidarr problem is about
        the whole library, and burying it in the tab that happens to be open
        would hide it from the user who caused it.
      */}
      <NoticeBanner />
      <Tabs
        value={activeTab}
        onValueChange={(value) => setTab(String(value))}
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
        {/*
          keepMounted here too: the library keeps where the user had browsed to
          in component state, and unmounting the panel would drop them back on
          the artist grid every time they looked at the Download tab.
        */}
        <TabsContent
          value="library"
          keepMounted
          className="min-h-0 flex-1 overflow-y-auto [&[hidden]]:hidden"
        >
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
