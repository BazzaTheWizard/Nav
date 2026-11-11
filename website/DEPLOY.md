# navstudiolab.com: how to update the live site

## Where the site actually lives
You don't need Base44. The live site at navstudiolab.com is not served by Base44 any more: it's the
page from `claude_review/website/` (the old "high-retention Shorts" version), uploaded to
**Cloudflare Pages** on your Cloudflare account. The domain's DNS is on Cloudflare too (the same
account that runs clips.navstudiolab.com). So updating it is a drag-and-drop in Cloudflare.

This folder (`website/`) is the new version: B2B LinkedIn positioning, the three plans, the
guarantee, the agency section, the free-clips form (it sends to your inbox through Web3Forms,
same key as before) and, since Round 55, two real sample clips playing in the hero (`samples/`).

## 1. Check it on your Mac first (1 minute)
Open `website/index.html` in Chrome (double-click it). The two sample clips should start playing
silently; click the speaker button on one to hear it from the start (the other mutes), click a video
to pause it. Scroll through on desktop, then make the window narrow to check the phone layout. To try
the form, fill it in: the request should arrive in your inbox.

## 2. Make the upload folder
```bash
mkdir -p ~/Desktop/navstudios-site
cd "/Users/barrynavan/Short form content project/website"
cp index.html og-image.png ~/Desktop/navstudios-site/
cp -R samples ~/Desktop/navstudios-site/
```
The folder must hold `index.html`, `og-image.png` and `samples/` (2 clips and 2 poster images, about
9 MB in all; Cloudflare Pages allows 25 MB per file). Don't upload this DEPLOY.md file.

## 3. Upload it to the existing project
1. Go to dash.cloudflare.com → **Workers & Pages**.
2. Open the Pages project that serves navstudiolab.com. To find it: each project lists its domains;
   it's the one showing **navstudiolab.com** under **Custom domains** (probably called `navstudios`).
3. Click **Create deployment** (or **Deployments → Upload assets**), drag in the `navstudios-site`
   folder, and click **Save and deploy**.
4. Open https://navstudiolab.com in a private window: you should see "The LinkedIn video desk for B2B executives"
   and the two sample clips playing. Check it on your phone too (they play inline, muted, with a sound button).

If you can't find the project: create a new one (**Create → Pages → Upload assets**), upload the
folder, then in its **Custom domains** tab add `navstudiolab.com`. Cloudflare will tell you if the
domain is attached to another project; remove it there first.

## 4. Add www (2 minutes, recommended)
Right now `www.navstudiolab.com` doesn't exist (people who type www get an error). In the same Pages
project → **Custom domains → Set up a custom domain** → `www.navstudiolab.com`. Cloudflare adds the
DNS record for you.

## 5. The sample clips (Round 55)
The hero shows two real pipeline edits as playable vertical videos, under **Sample executive cuts**,
each captioned "Sample cut from <show>", with the note "Spec edits from public episodes... Not client
work." They are spec work, not testimonials: keep that label on anything you add.

- `samples/sample-how-i-invest.mp4` + `.jpg` (How I Invest, "The #1 mistake in business sales")
- `samples/sample-y-combinator.mp4` + `.jpg` (Y Combinator, "Why software outsourcing shops are dead")

They are 720x1280 H.264 with AAC sound, `+faststart`, 3 to 5 MB each; the .jpg is the poster frame
(shown before playback, and instead of autoplay for visitors with reduced motion or data saver).
To swap one, ask me, or by hand (from this folder):
```bash
ffmpeg -i NEW_CLIP.mp4 -vf scale=720:1280:flags=lanczos -c:v libx264 -preset medium -crf 25 \
  -pix_fmt yuv420p -c:a aac -b:a 96k -movflags +faststart samples/sample-NAME.mp4
ffmpeg -ss 1.5 -i NEW_CLIP.mp4 -frames:v 1 -vf scale=720:1280 -q:v 4 samples/sample-NAME.jpg
```
then change the file names, title and show in the `<figure class="reel">` block in `index.html`.
These cuts come from other people's public episodes: if a show or speaker asks, take theirs down.
Once a client agrees (Omeed is the natural first), swap one for their clip and label it as theirs.

## Changing prices or the guarantee
They appear in three places that must agree (tests/test_round54.py checks it):
1. `core/outreach.py`: FOUNDING_PRICE, EXECUTIVE_PRICE, TEAM_PRICE, TEAM_SIZE, GUARANTEE
2. `cloudflare/clip-pages-worker.js`: OFFER (then paste the Worker into Cloudflare and deploy)
3. `website/index.html`: the Plans section and `PLANS` in the script at the bottom (then re-upload)
