# Does Lidarr's RescanFolders skip dot-prefixed folders?

Answered for Phase 7 (trash folder at `DOWNLOAD_PATH/.trash/<timestamp>/...`). Verified 2026-09-04 against a
live Lidarr, then cross-checked against Lidarr's source.

**Verdict: yes.** A dot-prefixed folder under a Lidarr root folder is invisible to the disk scan. No
`.trash` entry in Lidarr's ignore configuration is needed — and there is no folder ignore-list setting in
Lidarr to add one to.

## Version tested

`lscr.io/linuxserver/lidarr:latest`, image digest
`sha256:c74c32408fdf6e7926ad62641fc1a5544206ee65c33f2188bb179edb30e28f5a` (pulled 2026-09-04),
Lidarr **3.1.0.4875** on branch `master`, .NET 8.0.12, Alpine.

## Setup

```bash
S=<scratchdir>
M=$S/lidarr/music
mkdir -p "$M/Some Artist/Some Album" "$M/.trash/20260904T000000Z/Trashed Artist/Trashed Album"
cp track.flac "$M/Some Artist/Some Album/track.flac"
cp track.flac "$M/.trash/20260904T000000Z/Trashed Artist/Trashed Album/track.flac"

docker run -d --name lidarr-test -e PUID=0 -e PGID=0 -e TZ=Etc/UTC -p 18686:8686 \
  -v "$S/lidarr/config:/config" -v "$M:/music" lscr.io/linuxserver/lidarr:latest
```

`track.flac` is a real, decodable FLAC: 5.0 s of digital silence, mono, 44.1 kHz, 16-bit, built by hand
(STREAMINFO plus 54 constant-subframe frames) because ffmpeg was not installed, then tagged with Mutagen
(`title`, `artist`, `albumartist`, `album`, `tracknumber`, `date`). Both copies are byte-identical, so the
only difference between the two paths is the `.trash` segment.

`LogLevel` was raised to `trace` in `/config/config.xml` and the container restarted. API key was read from
the same file. Root folder added with:

```bash
curl -s -X POST -H "X-Api-Key: $K" -H 'Content-Type: application/json' \
  -d '{"name":"Music","path":"/music","defaultQualityProfileId":1,"defaultMetadataProfileId":1,
       "defaultMonitorOption":"none","defaultNewItemMonitorOption":"none","defaultTags":[]}' \
  http://localhost:18686/api/v1/rootfolder
```

## Commands run

```bash
# as the app sends it
curl -X POST .../api/v1/command \
  -d '{"name":"RescanFolders","folders":["/music"],"filter":"known","addNewArtists":false}'

# plain, no filter (Lidarr defaults it to "known")
curl -X POST .../api/v1/command -d '{"name":"RescanFolders","folders":["/music"]}'
```

Lidarr's own startup rescan (command id 1) ran with `filter: "none"` — the widest scan there is — so all
three filter levels are covered.

## Log excerpts (`/config/logs/lidarr.trace.txt`)

Startup rescan, `filter: none`:

```
2026-09-04 19:29:32.1|Trace|CommandExecutor|RescanFoldersCommand -> DiskScanService
2026-09-04 19:29:32.1|Info|DiskScanService|Scanning /music
2026-09-04 19:29:32.1|Debug|DiskScanService|Scanning '/music' for music files
2026-09-04 19:29:32.1|Trace|DiskScanService|1 files were found in /music
2026-09-04 19:29:32.1|Debug|DiskScanService|1 audio files were found in /music
2026-09-04 19:29:32.9|Debug|DiskScanService|Inserted 1 new unmatched trackfiles
```

Both explicit `RescanFolders` calls logged the same `1 files were found in /music` and inserted 0 further
files. Two `.flac` files exist under `/music`; Lidarr saw one. `grep -ic trash` over `lidarr.txt`,
`lidarr.debug.txt` and `lidarr.trace.txt` returns **0** — the path is never even mentioned.

## API cross-checks

```
GET /api/v1/trackfile?unmapped=true
  -> 1 result: /music/Some Artist/Some Album/track.flac

GET /api/v1/manualimport?folder=/music&filterExistingFiles=false
  -> 1 result: /music/Some Artist/Some Album/track.flac

GET /api/v1/manualimport?folder=/music/.trash&filterExistingFiles=false
  -> 1 result: /music/.trash/20260904T000000Z/Trashed Artist/Trashed Album/track.flac

GET /api/v1/filesystem?path=/music/&includeFiles=true
  -> lists ".trash" as a directory
```

The last two matter for understanding the mechanism rather than for the app. The exclusion is computed on
the path *relative to the folder being scanned*, so pointing Lidarr directly at `.trash` (or at a folder
inside it) does scan it — the dot segment is then above the base path. That never happens for us: the root
folder is `DOWNLOAD_PATH` and `.trash` always sits below it. The `filesystem` endpoint is the folder browser
for the UI, not the scanner, and lists everything.

## Source cross-check

`src/NzbDrone.Core/MediaFiles/DiskScanService.cs` (master):

```csharp
public static readonly Regex ExcludedSubFoldersRegex = new Regex(
    @"(?:\\|\/|^)(?:extras|@eadir|\.@__thumb|extrafanart|plex versions|\.[^\\/]+)(?:\\|\/)",
    RegexOptions.Compiled | RegexOptions.IgnoreCase);

public List<IFileInfo> FilterFiles(string basePath, IEnumerable<IFileInfo> files)
{
    return files.Where(file => !ExcludedSubFoldersRegex.IsMatch(basePath.GetRelativePath(file.FullName)))
                .Where(file => !ExcludedFilesRegex.IsMatch(file.Name))
                .ToList();
}
```

The `\.[^\\/]+` alternative is the general hidden-folder rule: any path segment starting with a dot is
excluded. `develop` carries the identical regex.

There is a second, earlier exclusion. `src/NzbDrone.Common/Disk/DiskProviderBase.cs`:

```csharp
return di.EnumerateFiles("*", new EnumerationOptions
{
    RecurseSubdirectories = recursive,
    IgnoreInaccessible = true
}).ToList();
```

`AttributesToSkip` is left at the .NET default of `Hidden | System`, and .NET on Unix reports dot-prefixed
files and directories as `Hidden`. That is why the trace says `1 files were found` rather than
`2 files were found ... 1 audio files were found`: the `.trash` tree is dropped during enumeration, before
the regex ever runs. Two independent layers, so the behaviour is not resting on one regex.

## Consequence for the README

No Lidarr ignore-list instruction is required. The README records the tested version and the finding. The
`.trash/.ndignore` file stays as belt-and-braces for Navidrome, which is a separate mechanism.
