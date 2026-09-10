# PDF Player 
PDF Player is a music practice tool that allows you to sync sheet music with an audio recording. PDF Player also doubles as a slow player providing users a useful tool for practicing and learning music. 
<br>

<figure align="center">
  <figcaption><b>Audio Timeline Tab.</b><br>Blue markers represent page markers, while red markers represent practice markers.</figcaption>
  <img src="audio_tab.png" width="400">
</figure>
<br>

<figure align="center">
  <figcaption><b>PDF Viewer Tab.</b><br> Pages can be navigated using Up/Down Arrow Keys.</figcaption>
  <img src="pdf_tab.png" width="400">
</figure>
<br>

## Download
Windows builds are attached to each [GitHub Release](https://github.com/funnydogeatapepsi/pdf_player/releases)
(built automatically by GitHub Actions from `build/pdf_player.spec`). To run from source:

```
pip install -r requirements.txt
python main.py
```

## Page offset
If your PDF has leading pages (cover, table of contents, ...), set the **page offset** in *Options → Project Options*
or hold **Ctrl** and scroll the mouse wheel over the PDF tab. The offset is the page shown before the first page
marker; page marker *k* then turns to page `offset + k`. The offset is saved with the project.

## Project files
Projects are saved as `.json` files (audio/PDF paths, marker positions, page offset). Older `.pkl` projects
still open; the next save writes a `.json` next to them.

## Slow-down method
*Options → Project Options* also lets you pick the time-stretch algorithm used by the playback speed slider:
**WSOLA** (default — overlap-add with waveform alignment), **Overlap-Add** (fastest), or
**Phase Vocoder**. This setting applies to all projects. Processing runs in the background — the status bar
shows *Processing audio...* and playback switches over when it is done, keeping your position.

## How to add markers
Page markers must be placed on the Audio Timeline in order to synchronize audio with page turns.
Clicking on the Audio Timeline with one of the corresponding combinations will add/remove a marker:

| Key                     | Function              |
|-------------------------|-----------------------|
| Shift + Left Click      | Add Practice Marker   |
| Ctrl + Left Click       | Add Page Marker       |
| Ctrl + Left/Right Click | Remove Marker        | 
| Hold A + Drag           | Move a marker         |

Practice markers are used to navigate to set locations within the Audio Timeline.
You can navigate to different markers using the following keys:

| Key                 | Function               |
|---------------------|------------------------|
| Right/Forward Arrow | Next Practice Marker | 
| Left/Back Arrow     | Previous Practice Marker     | 
| Up Arrow            | Next Page Marker   |
| Down Arrow          | Previous Page Marker   | 

