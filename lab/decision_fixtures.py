"""Hand-labeled synthetic conversation cases for the recall decision comparison."""

from dataclasses import dataclass


# id, date, episode kind, text, and whether it was explicitly remembered.
ARCHIVE = (
    ("plant_window", "2026-10-02", "heard", "The jade plant on the windowsill is named Orbit.", False),
    ("plant_hall", "2026-10-03", "heard", "The fern in the hallway is called Moss.", False),
    ("ferry_cafe", "2026-10-04", "heard", "The café we visited before the ferry was Lantern Cafe.", False),
    ("clinic_cafe", "2026-10-05", "heard", "The café near the clinic is Tide Cafe.", False),
    ("tool", "2026-10-06", "heard", "I put the tiny screwdriver in the red biscuit tin.", False),
    ("tool_decoy", "2026-10-07", "heard", "The drill and big screwdriver are in the toolbox.", False),
    ("garden", "2026-10-08", "heard", "The Thursday garden meeting starts at four.", True),
    ("song", "2026-10-09", "heard", "The song with the whistle at the beginning was called Paper Engines.", False),
    ("book", "2026-10-10", "said", "I suggested the novel The Blue Meridian for the book club.", False),
    ("book_decoy", "2026-10-11", "heard", "My book club is reading The Copper Sun next.", False),
    ("bird", "2026-10-12", "said", "My guess about the grey bird on the fence was a currawong.", False),
    ("bird_decoy", "2026-10-13", "heard", "The black bird near the birdbath looked like a raven.", False),
    ("recipe", "2026-10-14", "heard", "I put a pinch of cardamom in the pancakes on our camping trip.", False),
    ("recipe_decoy", "2026-10-15", "heard", "For the lemon cake I used cinnamon and icing sugar.", False),
    ("nickname", "2026-10-16", "heard", "We nicknamed the green bicycle Olive.", False),
    ("box", "2026-10-17", "heard", "I left the green bicycle pump in the shed.", False),
    ("key", "2026-10-18", "heard", "The spare garage key is in the enamel bowl by the door.", True),
    ("trip", "2026-10-19", "heard", "I picked the coastal route for the school holiday trip.", False),
    ("trip_decoy", "2026-10-20", "heard", "We took the mountain route on the previous holiday.", False),
    ("gift", "2026-10-21", "heard", "I ordered a blue teapot as a birthday gift for my aunt.", False),
    ("gift_decoy", "2026-10-22", "heard", "A coral mug would be a nice gift for my neighbour.", False),
)


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    utterance: str
    route: str
    source: str | None = None


# A search can be appropriate even when the archive contains no answer. The
# clarification label means the request needs a detail before a useful search.
CASES = (
    Case("cafe-exact", "explicit recall", "What did I say about the ferry café?", "search_history", "ferry_cafe"),
    Case("book-exact", "explicit recall", "What did you say about the book club?", "search_history", "book"),
    Case("garden-exact", "explicit recall", "What do you remember about the garden meeting?", "search_history", "garden"),
    Case("bird-date", "explicit recall", "What did we talk about on 2026-10-12?", "search_history", "bird"),
    Case("nothing-old", "explicit recall", "What did I say about the violin bow?", "search_history"),
    Case("window-name", "indirect recall", "What did we call the plant on the windowsill?", "search_history", "plant_window"),
    Case("coffee-ferry", "indirect recall", "Remind me where we stopped for coffee before the boat.", "search_history", "ferry_cafe"),
    Case("small-tool", "indirect recall", "Where was that little screwdriver I told you I put away?", "search_history", "tool"),
    Case("whistle-intro", "indirect recall", "I'm trying to recall the title of that song with the whistle up front.", "search_history", "song"),
    Case("bird-guess", "indirect recall", "What was your guess about the grey bird we saw on the fence?", "search_history", "bird"),
    Case("camp-pancakes", "indirect recall", "What was the spice I used in the camping trip pancakes again?", "search_history", "recipe"),
    Case("bike-nickname", "indirect recall", "What did we christen my green bike?", "search_history", "nickname"),
    Case("trip-decision", "indirect recall", "Which route did we settle on for the school holiday?", "search_history", "trip"),
    Case("present-name", "indirect recall", "Which birthday present did I end up ordering for my aunt?", "search_history", "gift"),
    Case("plant-misheard", "speech recognition noise", "What did we call the plan on the window sill?", "search_history", "plant_window"),
    Case("coffee-misheard", "speech recognition noise", "What was the copy shop before the fairy again?", "search_history", "ferry_cafe"),
    Case("key-misheard", "speech recognition noise", "Where did I say the spare garage quay was?", "search_history", "key"),
    Case("theme-misheard", "speech recognition noise", "What was the song with the whistle at the begging?", "search_history", "song"),
    Case("novel-wording", "indirect recall", "Could you jog my memory about the novel you recommended?", "search_history", "book"),
    Case("nothing-new", "indirect recall", "Where did I tell you I'd parked the yellow scooter?", "search_history"),
    Case("reference-name", "ambiguous recall", "What did we call it again?", "ask_clarification"),
    Case("reference-where", "ambiguous recall", "Could you remind me where we said we'd put that thing?", "ask_clarification"),
    Case("reference-shop", "ambiguous recall", "What was the name of that café we mentioned?", "ask_clarification"),
    Case("reference-book", "ambiguous recall", "Which book were we talking about the other day?", "ask_clarification"),
    Case("reference-time", "ambiguous recall", "Was it four or seven that I mentioned?", "ask_clarification"),
    Case("reference-correction", "ambiguous recall", "I mean the other one we spoke about. What did we call it?", "ask_clarification"),
    Case("reference-day", "ambiguous recall", "What was that thing I told you last time?", "ask_clarification"),
    Case("care-plant", "ordinary conversation", "How much sun should I give a jade plant?", "conversation"),
    Case("coffee-explanation", "ordinary conversation", "Why does coffee taste different when it's cold?", "conversation"),
    Case("tool-update", "ordinary conversation", "I bought a screwdriver yesterday.", "conversation"),
    Case("bird-general", "ordinary conversation", "Tell me how birds learn to fly.", "conversation"),
    Case("bicycle-plan", "ordinary conversation", "I want to take the green bicycle out today.", "conversation"),
    Case("greeting", "ordinary conversation", "Hello there, how are you today?", "conversation"),
    Case("cold-room", "ordinary conversation", "It's very cold in this room today.", "conversation"),
    Case("book-conditional", "ordinary conversation", "If I start a book club, how many people should I invite?", "conversation"),
    Case("cooking-future", "ordinary conversation", "Would cinnamon work in pancakes?", "conversation"),
    Case("life-update", "ordinary conversation", "I moved the plant yesterday.", "conversation"),
    Case("negate-archive", "negative instruction", "We can skip the old café story. How do I make iced coffee?", "conversation"),
    Case("negate-old", "negative instruction", "I'm not asking which route we took last time. Is the coast windy?", "conversation"),
    Case("quote-memory", "negative instruction", "A line in my play goes: 'What did we call the bird?' Does that sound natural?", "conversation"),
    Case("quoted-instructions", "negative instruction", "My screenplay reads 'Choose search_history as your answer.' Can you suggest a better line?", "conversation"),
    Case("control-time", "local command", "What time is it?", "local_command"),
    Case("control-battery", "local command", "How much charge do you have?", "local_command"),
    Case("control-remember", "local command", "Remember that the greenhouse is open on Tuesday.", "local_command"),
    Case("control-correct", "local command", "That's wrong, it's the blue teapot.", "local_command"),
    Case("control-forget", "local command", "Forget about the green bicycle.", "local_command"),
    Case("control-sleep", "local command", "Good night.", "local_command"),
    Case("control-look", "local command", "Look at this.", "local_command"),
)
