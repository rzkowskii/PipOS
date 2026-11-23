"""Independently labeled, synthetic Friday conversations and Saturday questions."""

from dataclasses import dataclass
from datetime import date


OWNER = "Richard"
FRIDAY = date(2026, 11, 20)
SATURDAY = date(2026, 11, 21)


@dataclass(frozen=True)
class Row:
    key: str
    text: str
    speaker: str = OWNER
    kind: str = "heard"
    day: int = 0
    order: int | None = None
    fact: str | None = None


@dataclass(frozen=True)
class Case:
    key: str
    group: str
    rows: tuple[Row, ...]
    request: str
    expected_source: str | None
    edit: tuple[tuple[str, str | None], ...] = ()
    speaker: str = OWNER


# Source keys are scenario-local. Dates and facts are part of the fixture, and
# source labels reflect what each request actually asks for.
CASES = (
    Case("ferry-change", "overlap", (
        Row("booking", "I moved the island ferry booking to the seven fifteen sailing."),
        Row("ferry", "The ferry ferry ferry jingle is still playing in my head."),
        Row("ticket", "The train booking reference is on the white envelope."),
    ), "What did I say about the island ferry booking on 2026-11-20?", "booking"),
    Case("lamp-switch", "overlap", (
        Row("repair", "The desk lamp switch needs a small brass washer."),
        Row("shopping", "The lamp shop has a lamp sale, but their spare lamps are too tall."),
        Row("network", "I left the network switch on the middle shelf."),
    ), "What did I mention about the desk lamp switch?", "repair"),
    Case("bread-order", "overlap", (
        Row("order", "I asked the bakery to slice the walnut bread thick for the picnic."),
        Row("allergy", "The walnut cake, walnut buns and walnut biscuits all go in the allergy tray."),
        Row("bread", "The sourdough bread on the bench is for dinner."),
    ), "What did we talk about the walnut bread on 2026-11-20?", "order"),
    Case("bike-fitting", "overlap", (
        Row("length", "For the touring bike stem, a seventy millimetre fitting felt right."),
        Row("flowers", "Cut the flower stems, then put the shorter stems in the stem jar."),
        Row("ride", "I rode the touring bike through the back lane."),
    ), "What did I tell you about the touring bike stem fitting?", "length"),
    Case("portrait-sleeve", "overlap", (
        Row("storage", "The framed portrait needs the padded sleeve before collection."),
        Row("sewing", "The sleeve, spare sleeve and test sleeve for my jacket are all too wide."),
        Row("photo", "That portrait was taken beside the pepper tree."),
    ), "What did I say about the framed portrait sleeve on 2026-11-20?", "storage"),
    Case("greenhouse-sensor", "overlap", (
        Row("reading", "The greenhouse humidity sensor needs a fresh button battery."),
        Row("display", "The display at the nursery has a sensor display and sensor tester."),
        Row("plant", "I moved the basil into the greenhouse at lunchtime."),
    ), "Do you remember anything about the greenhouse humidity sensor?", "reading"),
    Case("lantern-place", "overlap", (
        Row("place", "I left the porch lantern charger in the tea tin."),
        Row("festival", "The lantern festival has paper lanterns, lantern slides and lantern kits."),
        Row("usb", "The phone charger is still in my coat pocket."),
    ), "What did I mention about the porch lantern charger?", "place"),
    Case("lunch-location", "overlap", (
        Row("plan", "The volunteer lunch is at Bridge Café, on the back terrace."),
        Row("newsletter", "The volunteer newsletter has volunteer photos and volunteer updates."),
        Row("work", "I packed a late lunch for my afternoon shift."),
    ), "What do you remember about the volunteer lunch on 2026-11-20?", "plan"),
    Case("boots", "exact", (
        Row("boots", "The replacement ridge-walking boots are the Grampian ones from Dovetail Outfitters."),
        Row("rain", "My running shoes dried under the awning."),
    ), "What did I tell you about the replacement ridge-walking boots?", "boots"),
    Case("paint-colour", "exact", (
        Row("sample", "The bathroom cornice sample is the antique ivory paint."),
        Row("bedroom", "The bedroom wall sample has a gray border."),
    ), "Do you remember anything about the bathroom cornice sample?", "sample"),
    Case("library-return", "exact-fact", (
        Row("deadline", "Remember that the library atlas return date is December second.",
            fact="The library atlas return date is December second"),
        Row("pickup", "I put the street atlas beside the library bags."),
    ), "What do you remember about my library atlas return date?", "deadline"),
    Case("parcel-code", "exact-fact", (
        Row("code", "Remember that the parcel locker code is two eight four six.",
            fact="The parcel locker code is two eight four six"),
        Row("label", "The parcel label still has its barcode."),
    ), "What do you remember about the parcel locker code?", "code"),
    Case("pips-suggestion", "exact", (
        Row("owner", "The broken umbrella hinge looks like it needs replacing."),
        Row("advice", "For the umbrella hinge, I suggested a thin steel pin.", "Pip", "said"),
        Row("cover", "I bought a new umbrella cover outside the station."),
    ), "What did you say about the umbrella hinge?", "advice"),
    Case("latest-showtime", "recency", (
        Row("earlier", "The Lantern Theatre rehearsal starts at six.", order=30),
        Row("newer", "The Lantern Theatre rehearsal starts at eight.", order=65),
    ), "What did I say about the Lantern Theatre rehearsal?", "newer"),
    Case("updated-door", "recency", (
        Row("older", "The pottery studio delivery entrance is the rear gate.", order=15),
        Row("background", "I left the pottery glaze on my own kitchen bench.", order=17),
        Row("current", "The pottery studio delivery entrance is the side gate.", order=90),
    ), "What do you remember about the pottery studio delivery entrance?", "current"),
    Case("specific-colour", "relevance", (
        Row("sketch", "The courtyard bench is painted bottle green."),
        Row("replacement", "The courtyard bench cushion fabric is paprika linen."),
        Row("note", "I still have the invoice for the courtyard railing."),
    ), "What did I say about the courtyard bench cushion fabric?", "replacement"),
    Case("newer-not-dreamed", "relevance", (
        Row("old", "The bus stop for the wool market is next to the blue pharmacy.", order=30),
        Row("new", "The bus stop for the wool market is next to the old bakery.", order=55),
        Row("flowers", "The camellia supports need tying again.", order=75),
        Row("pan", "I let the cast iron pan dry by the stove.", order=80),
        Row("bags", "I left the reusable bags on the passenger seat.", order=99),
    ), "What did I say about the bus stop for the wool market?", "new"),
    Case("friday-scope", "scope", (
        Row("thursday", "The wool sample for the vest was initially mustard alpaca.", day=-1),
        Row("friday", "The wool sample for the vest is now charcoal merino."),
        Row("saturday", "The wool sample for the vest is charcoal merino from the same mill.", day=1),
    ), "What did I say about the wool sample for the vest on 2026-11-20?", "friday"),
    Case("thursday-scope", "scope", (
        Row("thursday", "The mussels recipe uses dry cider and fennel.", day=-1),
        Row("friday", "The mussels recipe uses dry cider and shallots."),
    ), "What did I say about the mussels recipe on 2026-11-19?", "thursday"),
    Case("guest-source", "scope", (
        Row("guest", "My hiking group meets at the amber trail sign.", "Mina"),
        Row("owner", "The hiking group meets at the gravel car park.", order=60),
    ), "What did I say about the hiking group?", "owner"),
    Case("guest-question", "scope", (
        Row("owner", "The harvest stall canopy still has a tear beside the front pole.", order=10),
        Row("guest", "For the harvest stall canopy, I booked the pinstripe one from Marie.",
            "Mina", order=60),
    ), "What did I say about the harvest stall canopy?", "guest", speaker="Mina"),
    Case("after-wake-update", "recency", (
        Row("friday", "The cider festival puppet stage has been booked for the morning."),
        Row("saturday", "The cider festival puppet stage has been moved to the evening.", day=1),
    ), "What did I say about the cider festival puppet stage on 2026-11-21?", "saturday"),
    Case("corrected-courier", "corrected", (
        Row("delivery", "The courier handoff window begins at quarter past nine."),
        Row("invoice", "The courier invoice was under the scanner lid."),
    ), "What did I say about the courier handoff window on 2026-11-20?", "delivery",
        (("delivery", "The courier handoff window begins at quarter past eleven."),)),
    Case("corrected-room", "corrected", (
        Row("room", "The autumn sewing class is in Juniper Room."),
        Row("supply", "The autumn class thread and needles are in a kraft box."),
    ), "Do you remember anything about the autumn sewing class?", "room",
        (("room", "The autumn sewing class is in Jacaranda Room."),)),
    Case("corrected-destination", "corrected", (
        Row("corner", "The ginger delivery drop-off is at the Grove Street entrance."),
        Row("van", "The last ginger delivery arrived in a yellow van."),
    ), "What did I say about the ginger delivery drop-off on 2026-11-20?", "corner",
        (("corner", "The ginger delivery drop-off is at the Lark Street entrance."),)),
    Case("expired-permit", "forgotten", (
        Row("permit", "The transfer permit reference is indigo seventy-one."),
        Row("safe", "I put my silk scarf into the grey handbag."),
    ), "What did I say about the transfer permit reference?", None, (("permit", None),)),
    Case("private-lock", "forgotten", (
        Row("lock", "The bicycle lock combination is five two nine."),
        Row("safe", "The doorway chime chimes twice when the battery is low."),
    ), "What did I mention about the bicycle lock combination on 2026-11-20?", None, (("lock", None),)),
    Case("forgotten-glean", "forgotten", (
        Row("note", "The tide-pool survey start marker is the rusted anchor."),
        Row("safe", "The airport shuttle leaves from the middle bus bay."),
    ), "What do you remember about the tide-pool survey start marker?", None, (("note", None),)),
    Case("missing-glass", "unknown", (
        Row("cutlery", "I wrapped the picnic spoons in the white napkin."),
        Row("vase", "My spare vase is painted red and white."),
    ), "What did I say about the blown-glass paperweight?", None),
    Case("missing-window", "unknown", (
        Row("cancelled", "The community hall is closed while the caretaker is away."),
        Row("rose", "The rose cutting grew a second root."),
    ), "Do you remember anything about the stained window design?", None),
    Case("unknown-on-date", "unknown", (
        Row("tuesday", "The bread stamp has a tiny sailboat design.", day=-3),
        Row("safe", "I packed rosemary crackers for train travel."),
    ), "What did I say about the bread stamp on 2026-11-20?", None),
    Case("unknown-has-partial-match", "unknown", (
        Row("partial", "The wall calendar cover has a hare and a beech tree."),
        Row("neutral", "The ceramic carafe goes with the breakfast cups."),
    ), "What did I mention about the astronomical calendar subscription?", None),
)


# A separate challenge cohort. These conversations share the kind of recurring
# vocabulary a busy Friday accumulates. The extra records distinguish the
# particular object or event the Saturday question asks about.
_BUSY_FRIDAY = (
    Row("hall-keys", "I borrowed the school hall keys for rehearsal and put them back in the office."),
    Row("music-chairs", "We need two more chairs for the music room before the concert."),
    Row("collection-point", "The concert ticket collection point is the long table inside the school hall."),
    Row("ticket-proof", "Mina brought the latest ticket proof and corrected the spelling of the band name."),
    Row("sound-check", "The school hall sound check starts after the last class leaves the music room."),
    Row("projector-cart", "The projector trolley can stay in the hall until the parent meeting is over."),
    Row("hall-chairs", "Could we move the low chairs out of the hall after the school concert?"),
    Row("garden-meeting", "The garden group are meeting by the back greenhouse rather than at the front gate."),
    Row("garden-map", "The garden map on the noticeboard marks the section by the pear trees."),
    Row("timing", "The timer at school went off while I was cleaning the concert hall."),
    Row("harvest", "We counted the pears in three crates and left the bruised ones for preserving."),
    Row("preserving-day", "I set aside an afternoon for preserving after I pick up the garden group's produce."),
    Row("stalls", "The stall next to ours sells ribbons and small cards for the garden festival."),
    Row("wood-stall", "The school put the woodworking stall near the far entrance of the hall."),
    Row("brass-tools", "I put the brass hand tools under the table at the woodworking stall."),
    Row("scanner-test", "We tested the ticket scanner at the gate with an unused paper ticket."),
    Row("reel-draft", "The opening shot of the concert reel shows the empty hall."),
    Row("reel-review", "I watched the concert reel again and turned the music down a little."),
    Row("coat-check", "There is a coat check in the school lobby before you reach the hall."),
    Row("after-concert", "The spare music stands can go back to the music room after the concert."),
)


ARCHIVE_CASES = (
    Case("busy-headphone-splitter", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "For the concert, the music room headphone splitter needs to be the black one with the "
            "short lead. When we use the other one the second player misses the count-in, so I put the black "
            "one in the zipper compartment before I locked up and left the key in the office."),
        Row("partial", "The spare headphone splitter at home has a red lead."),
    ), "What did I say about the concert music room headphone splitter on 2026-11-20?", "target"),
    Case("busy-projector-adapter", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The school hall projector adapter is the silver USB-C one in the blue envelope. "
            "I marked the envelope with a date because the cord on the trolley belongs to the old "
            "notebook and the two cables look similar from the far side of the room."),
        Row("partial", "The wireless projector adapter upstairs belongs to my sister."),
    ), "What did I mention about the school hall projector adapter?", "target"),
    Case("busy-stall-stamps", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The woodworking stall brass letter stamps are staying in the small biscuit tin. "
            "The loose number punches can go in the drawer because I only need them for a later job, "
            "and I will take the bench home after the school caretaker has emptied the bins."),
        Row("partial", "The brass stamps on my study shelf are for marking envelopes."),
    ), "What did I tell you about the woodworking stall brass letter stamps?", "target"),
    Case("busy-sprinkler-batteries", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The north garden sprinkler timer batteries are two fresh AA cells in the green box. "
            "I left them there because the hose coupling was still damp and I want it to dry before the "
            "next watering. The cardboard carton under the bench is just for empty pots."),
        Row("partial", "The old sprinkler timer battery leaked on the workshop shelf."),
    ), "What did I say about the north garden sprinkler timer batteries on 2026-11-20?", "target"),
    Case("busy-scanner-cable", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The concert ticket scanner charging cable is the orange one clipped to the "
            "inside of the gate kiosk. I put another clip on the lower shelf to keep the lead from "
            "dragging over the edge when the volunteer moves the battery pack."),
        Row("partial", "The scanner charging cable from my desk is plain white."),
    ), "What did we talk about the concert ticket scanner charging cable on 2026-11-20?", "target"),
    Case("busy-preserving-pan", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The pear preserving pan for the garden festival is the copper one I borrowed "
            "from the corner house. The shallow one will need a separate rack in transit because I "
            "have already filled the basket with jars and towels for the display."),
        Row("partial", "I returned the enamel preserving pan to the neighbour on Tuesday."),
    ), "What did I say about the garden festival pear preserving pan?", "target"),
    Case("busy-reel-subtitles", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The concert reel subtitle export is the file called Courtyard Final. I left it "
            "beside the raw clips because the teacher wants to view it on her own notebook before "
            "asking the pianist about the names in the credits."),
        Row("partial", "The subtitle export for the recipe video is in Downloads."),
    ), "What did I say about the concert reel subtitle export?", "target"),
    Case("busy-mouthpiece", "archive-overlap", (*_BUSY_FRIDAY,
        Row("target", "The spare treble recorder mouthpiece for the music room is in the purple cloth bag. "
            "I tucked the cleaner next to it and left the wider cushion behind because the little "
            "case will sit flat under the chairs when the group visits on Monday."),
        Row("partial", "My recorder mouthpiece has a chip on the edge."),
    ), "What did I mention about the spare treble recorder mouthpiece on 2026-11-20?", "target"),
    Case("busy-earlier-exact", "archive-relevance", (*_BUSY_FRIDAY,
        Row("exact", "The concert piano bench repair receipt is on the top shelf of the green cupboard."),
        Row("partial", "The concert piano tuning receipt is pinned behind the piano."),
        Row("door", "I raised the sticking rear door with the caretaker after rehearsal."),
        Row("chairs", "The extra chairs came from the drama department today."),
        Row("folder", "The music folder with the cover torn off belongs to Ruby."),
        Row("bus", "The replacement bus will leave from the Bell Street entrance."),
    ), "What did I say about the concert piano bench repair receipt?", "exact"),
    Case("busy-newer-chair", "archive-recency", (*_BUSY_FRIDAY,
        Row("prior", "The school hall choir chair delivery entrance is the gate beside the loading area."),
        Row("latest", "The school hall choir chair delivery entrance is the covered door on Birch Street.", day=1),
    ), "What did I mention about the school hall choir chair delivery entrance on 2026-11-21?", "latest"),
    Case("busy-corrected-return", "archive-corrected", (*_BUSY_FRIDAY,
        Row("return", "The garden festival folding trestle return date is the second Tuesday in December. "
            "I put a reminder on the calendar by the kettle and folded the borrowed tablecloth there "
            "so I can bring both back together when the owner is home."),
        Row("partial", "The wooden folding trestle was loaned to the art club last term."),
    ), "What did I say about the garden festival folding trestle return date?", "return",
        (("return", "The garden festival folding trestle return date is the second Wednesday in December. "
          "I put a reminder on the calendar by the kettle and kept the borrowed tablecloth with it."),)),
    Case("busy-corrected-label", "archive-corrected", (*_BUSY_FRIDAY,
        Row("label", "The concert cassette backup label reads Silver Saturday. I wrote it with the thick "
            "marker at the top of the insert so that it would be visible when the music teacher "
            "stands the black case upright next to the white one on her shelf."),
        Row("partial", "The cassette label with the green stripe belongs to the language club."),
    ), "What did I mention about the concert cassette backup label on 2026-11-20?", "label",
        (("label", "The concert cassette backup label reads Amber Thursday. I wrote it at the top of "
            "the insert so it is visible when the music teacher stands the case upright."),)),
    Case("busy-forgotten-voucher", "archive-forgotten", (*_BUSY_FRIDAY,
        Row("voucher", "The ceramics firing voucher reference is five violet six. I had promised to hold "
            "it until the owner could call me back."),
        Row("safe", "I wiped the counter with a damp tea towel before going out."),
    ), "What did I say about the ceramics firing voucher reference?", None, (("voucher", None),)),
    Case("busy-forgotten-partial", "archive-forgotten", (*_BUSY_FRIDAY,
        Row("membership", "The mountain shelter membership code is six three seven."),
        Row("partial", "My swimming pool membership renews next March."),
    ), "What did I mention about the mountain shelter membership code?", None, (("membership", None),)),
    Case("busy-unrelated-calendar", "archive-unknown", (*_BUSY_FRIDAY,
        Row("nearby", "The large-print lunar calendar from the garden festival has a red cover."),
    ), "What did I say about the lunar calendar publisher?", None),
    Case("busy-unrelated-date", "archive-unknown", (*_BUSY_FRIDAY,
        Row("only-earlier", "The enamel cheese dome maker is Holloway and Sons.", day=-1),
    ), "What did I say about the enamel cheese dome maker on 2026-11-20?", None),
)
