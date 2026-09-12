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
**WSOLA** (default — overlap-add with waveform alignment, no wobble), **Overlap-Add** (fastest), or
**Phase Vocoder**. This setting applies to all projects. Processing runs in the background — the status bar
shows *Processing audio...* and playback switches over when it is done, keeping your position.

## Metronome
Press **M** to enter *metronome edit mode*: page/practice markers are hidden and the timeline shows only the
green **tempo markers** and the beat grid they define (tall ticks on downbeats). In this mode:

| Key                        | Function                                  |
|----------------------------|-------------------------------------------|
| Shift + Left Click         | Add a tempo marker (or Shift + Space at the scrubber) |
| Right Click on marker      | Edit its tempo (bpm) and beats per bar    |
| Ctrl/Shift + Right Click   | Remove a tempo marker                     |
| Hold A + Drag              | Move a tempo marker                       |

Each tempo marker holds until the next one, so tempo and time-signature changes are just more markers.
**Ctrl+M** toggles the click track (level in *Options → Project Options*); the toolbar shows the current bar and
beat. Clicks are rendered into the audio, so they stay locked to the music at any playback speed.

## How to add markers
Page markers must be placed on the Audio Timeline in order to synchronize audio with page turns.
Clicking on the Audio Timeline with one of the corresponding combinations will add/remove a marker:

| Key                     | Function              |
|-------------------------|-----------------------|
| Shift + Left Click      | Add Practice Marker   |
| Ctrl + Left Click       | Add Page Marker       |
| Ctrl + Left/Right Click | Remove Marker        | 
| Hold A + Drag           | Move a marker         |
| Ctrl + Scroll Wheel     | Zoom the timeline (1x - 16x; the view scrolls and follows the scrubber) |

Practice markers are used to navigate to set locations within the Audio Timeline.
You can navigate to different markers using the following keys:

| Key                 | Function               |
|---------------------|------------------------|
| Right/Forward Arrow | Next Practice Marker | 
| Left/Back Arrow     | Previous Practice Marker     | 
| Up Arrow            | Next Page Marker   |
| Down Arrow          | Previous Page Marker   | 

