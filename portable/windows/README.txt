Discord Music Bot - portable version (no installer)
====================================================

1. Install Python 3.10 or newer if you don't have it:
     winget install Python.Python.3.13
   (or from https://www.python.org/downloads/)

2. Double-click start.cmd.
   The first time, it sets up the bot in this folder (a minute or two), asks for your
   bot token, and offers to install FFmpeg and Deno. Then the bot runs in that window;
   close the window or press Ctrl+C to stop it. If it crashes, it restarts by itself.

   Get the token at https://discord.com/developers/applications -> your app -> Bot ->
   Reset Token. It's saved in the .env file in this folder: keep that file private.

3. Optional: start the bot when you log in to Windows. In this folder, run:
     start.cmd --autostart on
   (start.cmd --autostart off undoes it)

Moving or deleting this folder: run "start.cmd --autostart off" first if you turned it on.

If Windows blocks start.cmd after downloading the zip: right-click the zip ->
Properties -> tick "Unblock" -> OK, then extract it again.

Only run one copy of the bot: two copies with the same token both answer commands.
