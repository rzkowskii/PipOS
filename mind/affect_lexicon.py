"""Word lists and rules for mind.affect: a short, high-precision whitelist.

Data only. mind/affect.py compiles it and does the matching.

Pattern syntax (one string per pattern, elements separated by spaces):
    word            the token must be exactly this word
    a|b|c           one of several alternatives
    good_morning    underscores join several tokens into one alternative
    @NAME           any one token from the named set below
    _               any one ordinary token (a pet's name, a school subject)
    element?        the element is optional
Between elements up to two intensifier tokens (INTENS) may be skipped, so
"i'm so sad" and "i'm kind of sad" match the pattern "i'm @SAD".

Every rule has the shape
    (name, family, strength, position, tail, question_ok, leads, not_next,
     clause_boost, patterns)
position   start         the match begins the clause (after a few lead-in words)
           start_or_end  or it closes the clause (thanks)
           any           anywhere in the clause, but no negation or reported
                         speech in the three words before it
tail       what may follow the match inside the clause: any, tight (only
           vocatives and fillers), tightlast (tight, and nothing else in the
           sentence either), vtime (only a vocative or a time filler: "pip",
           "yesterday", "last night"; the rule for loss and crying), gasp (nothing,
           or "really"), greet (vocatives, then nothing or one phrase of the CLOSED
           list GREET_FOLLOW), thanks (padding, or "for" plus a whitelisted noun or
           gerund) or noway (the "no way" rule: the next word must be "really", ...)
question_ok  False, True, or "rest" (a question is fine when the match is not
           the whole clause, as in "good morning, how are you?")
Whatever a rule matches, mind/affect.py only lets a read() through when the
WHOLE utterance is the cue plus filler (the short-utterance rule; STRIP,
TURN_WORDS and SAFE_CLAUSES below are its data).
Families are the ten ids of the shared contract; this module emits warm,
humour, worry, support, surprise and thanks only. Nothing here maps a sense or
a body state ("I see", "I hear", "I feel hot", "I'm tired" are never rules).
All text in comments and tests is synthetic.
"""

# Words the person may say before a clause's real content ("well, I'm sad").
FILLERS = frozenset((
    "well", "so", "and", "um", "uh", "hmm", "hm", "ok", "okay", "yeah", "yes",
    "pip", "actually", "honestly", "just", "now", "anyway", "today", "tonight",
    "lately", "right",
))
# Calling Pip or the room by name, after a greeting or thanks.
VOCATIVES = frozenset(("pip", "there", "again", "everyone", "buddy", "friend", "mate"))
# What may follow a greeting (or good night) inside its own clause: a CLOSED list of whole
# phrases, nothing else. "hello, what's your problem" and "good morning, how is this useful"
# are jabs, so no open "how" or "what" is allowed; a phrase may be followed by vocatives or
# a time word ("how are you today pip").
GREET_FOLLOW = frozenset((
    ("how", "are", "you"), ("how", "are", "you", "doing"), ("how", "you", "doing"),
    ("how's", "it", "going"), ("how", "did", "you", "sleep"), ("how", "was", "your", "day"),
    ("how's", "everything"), ("how", "have", "you", "been"), ("what's", "up"), ("what's", "new"),
    ("see", "you", "tomorrow"), ("see", "you", "later"), ("see", "you", "soon"),
    ("sleep", "well"), ("sweet", "dreams"),
))
# Lead-in words that do not stop a clause from being a question ("so, what is ...").
QLEADS = frozenset(("well", "so", "and", "um", "uh", "hmm", "hm", "ok", "okay",
                    "yeah", "yes", "pip", "anyway"))
# A story, not a life: "my dog died in the movie" is not a loss.
FICTION = frozenset(("game", "movie", "film", "show", "story", "book", "episode",
                     "series", "dream", "play", "novel", "scene", "cartoon", "anime"))
# Aimed at Pip: "I'm upset with you", "I'm scared of you" are not a worry to soothe.
YOU = frozenset(("you", "you're", "your", "you'll", "you've", "yours", "pip"))
YOU_NOT_PIP = YOU - frozenset(("pip",))
REJECT_ANY = {
    "loss": FICTION,
    "upset": YOU,
    "sad": YOU_NOT_PIP,
    "down": YOU_NOT_PIP,
    "crying": YOU_NOT_PIP | frozenset(("onion", "onions")),
    # fear is not anticipation, joy or a denial ("I'm scared of nothing")
    "anxious": YOU_NOT_PIP | frozenset(("joy", "excited", "excitement", "thrilled",
                                        "nothing", "happy")),
    # a failed build or test run is work, not an exam
    "failed": YOU_NOT_PIP | frozenset(("build", "builds", "ci", "lint", "linter", "unit",
                                       "integration", "regression", "pipeline", "suite",
                                       "e2e", "smoke", "deploy", "compile", "tests")),
    # a wish for luck at harming something is not a wish for luck
    "lost_job": YOU_NOT_PIP,
    "luck": frozenset(("kill", "killing", "delete", "deleting", "destroy", "destroying",
                       "hurt", "hurting", "cheat", "cheating", "fight", "fighting",
                       "erase", "erasing", "beat", "beating", "break", "breaking")),
}
# What may trail a short exclamation or compliment ("good job pip", "that's funny too").
TIGHT = frozenset((
    "pip", "there", "again", "too", "today", "buddy", "friend", "mate", "really",
    "so", "much", "one", "little", "guy", "always", "now", "as", "ever",
))
# Only a vocative or a time filler may follow "died", "passed away", "I lost my dog" or
# "I'm crying": "yesterday", "last week", "this morning". Anything else ("in minecraft",
# "of embarrassment", "at the mall", "at this video") is a game, an idiom or another
# story. "again", "twice" and "there" are not vocatives here ("my sister died again").
VTIME = frozenset((
    "pip", "buddy", "friend", "mate", "today", "tonight", "yesterday", "lately", "this",
    "last", "morning", "afternoon", "evening", "night", "week", "weekend", "month", "all",
    "day",
))
# What may trail a closing "thanks".
ENDTAIL = frozenset(("pip", "again", "so", "much", "very", "a", "lot", "everyone"))
# After "no way": words that make it astonishment rather than a refusal.
NOWAY_NEXT = frozenset((
    "really", "seriously", "that's", "that", "you", "you're", "did", "is", "are",
    "it's", "wow", "way",
))
# Tokens that may sit inside a pattern as softeners or boosters.
INTENS = frozenset((
    "so", "really", "very", "too", "pretty", "quite", "extremely", "super",
    "truly", "just", "still", "kinda", "kind", "of", "sort", "a", "bit", "little",
    "slightly", "somewhat", "completely", "utterly", "incredibly", "terribly",
    "deeply", "much",
))
BOOST = frozenset((
    "so", "really", "very", "extremely", "super", "truly", "completely",
    "utterly", "incredibly", "terribly", "deeply", "pretty", "quite",
))
SOFT = frozenset(("kinda", "kind", "sort", "bit", "little", "slightly", "somewhat"))

# A clause that opens with one of these is a question, whatever its punctuation.
QWORDS = frozenset((
    "why", "what", "what's", "how", "how's", "when", "where", "who", "whom",
    "whose", "which", "are", "is", "am", "do", "does", "did", "can", "could",
    "would", "should", "will", "shall", "have", "has", "had", "was", "were",
    "isn't", "aren't", "don't", "doesn't", "didn't", "won't", "can't",
    "wouldn't", "couldn't", "shouldn't",
))
# Negation and "not really meant" words: a match with one of these in the three
# words before it is dropped.
NEGATION = frozenset((
    "not", "no", "never", "nothing", "nobody", "hardly", "barely", "without",
    "cannot", "neither", "nor", "none", "dont", "cant", "wont", "isnt", "arent",
    "wasnt", "didnt", "doesnt", "couldnt", "wouldnt", "shouldnt", "aint", "ain't",
))
HYPOTHETICAL = frozenset((
    "if", "when", "whenever", "unless", "whether", "pretend", "imagine",
    "suppose", "would", "could", "might", "maybe", "perhaps", "hope",
))
REPORTED = frozenset((
    "say", "said", "says", "saying", "tell", "told", "tells", "write", "wrote",
    "to", "will", "should", "must", "gonna", "wanna", "can", "i'll", "you'll",
    "we'll", "he'll", "she'll", "they'll",
))
# A positive reaction (warm, thanks, humour, surprise) is dropped when one of
# these sits elsewhere in the same sentence: the person turns on it ("thank you
# for the update, but that was wrong", "wow, you broke it", "lol you are so
# wrong"). "no" is left out on purpose: "no way!" is a rule word.
POSITIVE_FAMILIES = frozenset(("warm", "thanks", "humour", "surprise"))
NO_CONTRAST_VETO = frozenset(("kidding",))
CONTRAST = (NEGATION - frozenset(("no",))) | frozenset((
    "but", "however", "though", "although", "yet", "still", "wrong", "broke",
    "broken", "break", "bad", "terrible", "awful", "useless", "worse", "worst",
    "stupid", "dumb", "idiot", "annoying", "annoyed", "angry", "mad", "hate",
    "shut", "worthless", "pathetic", "ridiculous", "rubbish", "garbage", "lame",
    "slow", "failed", "ruined", "ruining", "ruin", "mistake", "mess", "ugh",
    "deleted", "deleting", "erased", "lost", "crashed", "killed", "wasted", "forgot",
    "forgotten", "missed", "woke", "interrupted", "ignore", "ignored", "ignoring",
    "cry", "lying", "lie", "liar", "lied",
    "fool", "foolish", "jerk", "loser", "moron", "trash", "crap", "crappy", "sucks", "suck",
    "stinks", "stink", "disgusting", "gross", "ew", "boring", "nonsense", "waste", "hopeless",
    "joke", "silly",
))
# Conditions and "said X" tags: they undo a compliment, thanks or laugh, but a
# greeting may be followed by them ("good morning, if it rains tell me").
CONDITION = frozenset(("if", "when", "unless", "except", "said", "says", "while"))
# A greeting is dropped when a later clause opens as an accusation or a command
# ("good morning, you woke me up", "hello, stop talking").
GREETING_RULES = frozenset(("greeting", "goodnight"))
GREETING_ONLY = frozenset(("greeting",))   # exempt from CONDITION words
ACCUSE_START = frozenset(("you", "you're", "your", "you've", "you'll", "why", "stop",
                          "quit", "enough", "shut", "where"))
# A worry is dropped when the same sentence has a clause that opens with "you"
# and holds a CONTRAST word: "I'm so upset, you ruined it".
YOU_START = frozenset(("you", "you're", "your", "you've", "you'll"))
# The wildcard "_" never matches these (so "my dog barked and died" cannot match).
WILD_STOP = frozenset(("and", "but", "said", "says", "who", "that", "which", "because", "or",
                       "almost", "nearly", "again", "twice", "also", "ever", "even"))

# ---- the short-utterance rule (mind/affect.py read) ------------------------------------
# read() reacts only when the WHOLE utterance is the cue plus filler. These sets are its data.
# Filler and vocatives: words that never make a sentence "something else". They are left
# out of the count when asking how much of a sentence the cue covers, and they never open
# an uncovered stretch. "now", "the", "that", "you" are NOT filler: they turn a sentence.
STRIP = frozenset((
    "pip", "please", "so", "very", "really", "just", "oh", "hey", "um", "uh", "hmm", "hm",
    "well", "ok", "okay", "yeah", "yes", "actually", "honestly", "seriously", "there", "again", "buddy",
    "friend", "mate", "everyone", "too", "much", "lot",
    "today", "tonight", "tomorrow", "yesterday", "lately",
    # short interjections that lead a thanks or a statement ("great, thanks", "wow, my dog died")
    "great", "cool", "perfect", "awesome", "nice", "lovely", "aw", "aww", "wow",
))
# A stretch of the sentence the cue does not cover (or a whole uncovered clause) may not
# open with one of these: a negation, a contrast, a condition, "and", "that", "it",
# "you", "now", "the", "then", a third person, or an imperative. It then turns the line
# ("that was not what I asked", "now fix it", "you always do this", "the meeting ...").
IMPERATIVES = frozenset((
    "stop", "quit", "fix", "shut", "put", "turn", "set", "play", "tell", "show", "give",
    "make", "go", "come", "look", "listen", "wait", "hold", "open", "close", "cancel",
    "leave", "be", "do", "try", "let", "call", "send", "read", "remind", "add", "check",
    "say", "help", "get", "take", "keep", "bring", "find", "forget", "ignore", "answer",
    "repeat", "explain", "speak", "talk", "move", "pick", "drop", "delete", "remove",
    "change", "switch", "start", "restart", "reset", "clear", "mute", "wake", "pause",
    "skip", "repeat", "work", "behave", "calm", "relax", "hurry", "shush", "hush",
))
TURN_WORDS = (NEGATION | IMPERATIVES | frozenset((
    "nope", "but", "however", "though", "although", "yet", "still", "except", "unless",
    "if", "when", "whenever", "whether", "because", "cause", "since", "while", "maybe",
    "perhaps", "that", "that's", "these", "those", "it", "it's", "you", "you're",
    "your", "you've", "you'll", "you'd", "yours", "now", "then", "the", "also", "why",
    "he", "she", "they", "he's", "she's", "they're", "his", "her", "their", "said", "says",
    "and",
)))
# Nothing may come BEFORE the cue except filler ("dinner is late, thanks" is None).
# What follows the cue in the same clause is detail, and its first word must be a
# preposition, a time word, "a", "out" or a question opener ("at work", "about it",
# "last night", "how did you sleep"); any other word ("obama", "gross", "so what")
# is something else being said. ("what a ..." is an exclamation, never a question.)
GAP_START_OK = frozenset((
    "at", "on", "in", "about", "with", "for", "from", "of", "over", "after", "before",
    "during", "since", "last", "this", "all", "right", "a", "an",
    "how", "what", "what's", "can", "could", "did", "do", "does", "is", "are", "will", "would",
))
# Words that turn a stretch wherever they stand in it ("at work, now fix it").
GAP_STOP = frozenset(("now", "stop", "quit", "shut", "enough", "then", "forget", "ignore",
                      "leave", "cancel", "delete", "fix", "never"))
# Two-word openers that turn a stretch although the first word is allowed above.
GAP_BAD_PAIRS = frozenset((("what", "a"), ("what", "an"), ("this", "is"), ("this", "was"),
                           ("this", "has"), ("this", "isn't"), ("this", "sucks"),
                           ("how", "could"), ("how", "dare"), ("how", "come")))
# A whole clause with nothing from the cue in it must be exactly one of these phrases (a
# question after a greeting, "see you tomorrow", "how about you"); anything else, including a
# second claim about oneself ("I'm crying, I'm cutting onions") or an open "how" or "what"
# ("what's your problem"), makes the whole read None. STRIP words (pip, today, ...) are
# ignored when comparing.
SAFE_CLAUSES = GREET_FOLLOW | frozenset((
    ("how", "about", "you"), ("who's", "there"),
))
# What may trail one of the phrases above, or a closing: a vocative or a time word.
PHRASE_TAIL = VTIME | frozenset(("everyone", "tomorrow", "please"))
# After a thanks: a short closing that says the help worked. Whole clause, or the end of the
# thanks clause; "that helped, but ..." never matches.
CLOSINGS = frozenset((
    ("that", "helped"), ("that", "worked"), ("that", "was", "helpful"), ("that", "helps"),
    ("that", "helped", "a", "lot"), ("that", "was", "helpful", "a", "lot"),
))
# A hesitation or a shrug in front of thanks makes it grudging ("well, thanks", "uh, thank
# you", "hmm thanks"). They stay in STRIP for coverage, but a thanks hit after one is dropped.
HESITATION = frozenset(("well", "uh", "um", "hmm", "hm", "meh"))
# "thanks" may be padded ("thank you so much pip") or followed by "for" and a whitelisted
# noun or gerund ("thanks for the help", "thanks for coming"). "thanks for <anything
# else>" ("thanks for waking up the baby") is not thanks.
THANKS_PAD = ENDTAIL | VOCATIVES | frozenset(("really", "truly", "all"))
THANKS_DET = frozenset(("the", "your", "my", "all", "a", "an", "this", "our"))
THANKS_FOR_OK = frozenset((
    "help", "helping", "coming", "listening", "telling", "reminder", "reminding", "update",
    "updating", "answer", "answers", "info", "information", "advice", "tip", "tips",
    "suggestion", "suggestions", "company", "time", "hug", "visit", "support", "supporting",
    "patience", "understanding", "kindness", "effort", "everything", "yesterday", "today",
    "tonight", "music", "song", "joke", "jokes", "story", "stories", "waiting", "trying",
    "checking", "caring", "sharing", "explaining", "teaching", "cheering",
))
THANKS_FOR_TAIL = frozenset(("me", "us", "out", "with", "this", "it", "today", "yesterday",
                             "tonight", "pip", "again", "so", "much", "too", "a", "lot"))

# Sarcasm and irony markers: anywhere in the text they silence the whole read.
SARCASM_WORDS = frozenset(("genius", "einstein", "sherlock", "/s", "sarcasm", "sarcastic",
                           "sarcastically", "ironic", "ironically", "whatever"))
SARCASM_PHRASES = tuple(tuple(p.split()) for p in (
    "for nothing", "yeah right", "yeah sure", "oh great", "oh wonderful",
    "oh brilliant", "oh fantastic", "oh perfect", "oh super", "oh joy",
    "oh thanks", "oh very funny", "how nice", "how funny", "as if", "nice going",
    "good going", "real funny", "ha ha very funny", "haha very funny",
    "very funny pip", "gee thanks", "thanks for that", "thanks for ruining",
    "thanks for breaking", "thanks for killing", "sure thanks", "wow great",
    "wow thanks a lot", "wow really", "wow nice", "just great", "real nice",
    "big deal", "thanks a lot for nothing", "oh thanks a lot", "thank you for nothing",
    "good one genius", "thanks i guess", "thanks anyway", "thanks now", "thanks a lot now",
    "thanks a lot you", "thanks pip now", "thank you now", "no thanks", "no thank you",
    "not funny", "not amused", "not impressed",
    # a trailing "said X" tag turns a compliment or a love line into a sneer
    "no one ever", "nobody ever", "said no one", "said nobody", "said no-one",
    "no one said", "nobody said", "said noone", "said anyone ever", "said no one ever",
    "said nobody ever",
))
# "thanks a lot" after a worry or fear is irony ("I'm anxious, thanks a lot").
IRONIC_THANKS = frozenset((("thanks", "a", "lot"), ("thanks", "a", "bunch"),
                           ("thanks", "a", "million")))

SETS = {
    "SAD": ("sad", "unhappy"),
    "LONELY": ("lonely",),
    # "upset" is mild and often aimed at someone: concern, never the sad pose
    "UPSET": ("upset",),
    "SAD_STRONG": ("miserable", "depressed", "heartbroken", "gutted", "devastated"),
    "DOWN": ("down", "low", "blue", "gloomy"),
    "ANX": ("nervous", "anxious", "stressed", "worried", "scared"),
    "ANX_STRONG": ("terrified", "petrified"),
    "ROUGH": ("rough", "bad", "terrible", "awful", "horrible", "tough", "crappy",
              "lousy", "painful", "difficult", "stressful", "miserable"),
    "DAYN": ("day", "week", "month", "year", "night", "morning", "evening",
             "weekend", "afternoon"),
    "OLDADJ": ("old", "little", "best", "dear", "beloved", "poor", "pet", "lovely",
               "sweet", "favourite", "favorite", "elderly", "oldest"),
    "KIN": ("dog", "cat", "puppy", "kitten", "pet", "hamster", "rabbit", "horse",
            "parrot", "mom", "mum", "mother", "dad", "father", "grandma",
            "grandmother", "grandpa", "grandfather", "granddad", "grandad", "nan",
            "nana", "gran", "brother", "sister", "uncle", "aunt", "auntie",
            "husband", "wife", "friend", "cousin", "son", "daughter", "partner",
            "boyfriend", "girlfriend"),
    "MISSKIN": ("dog", "cat", "mom", "mum", "mother", "dad", "father", "grandma",
                "grandpa", "family", "friends", "parents", "kids", "children",
                "grandparents", "brother", "sister", "husband", "wife",
                "hometown", "friend"),
    # no bare "test": a failed test is usually a unit test; "my driving test" is TESTKIND + test
    "EXAMS": ("exam", "exams", "class", "course", "interview", "finals",
              "quiz", "midterm", "assignment", "audition"),
    "TESTKIND": ("driving", "math", "maths", "spelling", "science", "history", "biology",
                 "chemistry", "physics", "english", "french", "spanish", "school",
                 "entrance", "written", "oral", "practical", "theory"),
    "FUNNY": ("funny",),
    "HILARIOUS": ("hilarious", "hysterical"),
    "DELIGHT": ("amazing", "incredible", "awesome", "fantastic", "wonderful"),
    "PRAISE_ADJ": ("amazing", "awesome", "brilliant", "wonderful", "fantastic",
                   "incredible", "great", "best", "lovely", "sweet", "adorable", "cute"),
    "PRAISE_DONE": ("great", "well", "amazing", "awesome", "brilliant", "fantastic"),
    # Laughter: "haha"-style tokens are generated here, not listed one by one.
    "LAUGH_SMALL": ("haha", "ahaha", "hahah", "hehe", "hehehe", "heheh", "lol"),
    "LAUGH_BIG": ("hahaha", "hahahaha", "hahahah", "ahahaha", "bahaha", "muahaha",
                  "lmao", "lmfao", "rofl", "roflmao"),
}

# Tokens that count as laughter for the "this sentence is a joke" conflict check.
LAUGH_TOKENS = frozenset(SETS["LAUGH_SMALL"]) | frozenset(SETS["LAUGH_BIG"]) | frozenset((
    "laughing", "laughter", "laugh", "funny", "hilarious", "hysterical", "ha",
))

TL = FILLERS | frozenset(("oh", "aw", "aww", "wow", "great", "cool", "perfect",
                          "awesome", "nice", "lovely"))
GL = FILLERS | frozenset(("oh",))

# (name, family, strength, position, tail, question_ok, leads, not_next, boost, patterns)
THANKS_BOOST = (frozenset(("much", "lot", "really", "truly", "so", "very")), 0.30)
PRAISE_BOOST = (frozenset(("great", "excellent", "amazing", "awesome", "brilliant", "fantastic")), 0.10)
NO_WORDS = frozenset()
# "nervous about / to / with / and ..." is anticipation, a target or a mixed feeling
ANX_NOT_NEXT = frozenset(("but", "with", "to", "and", "about"))
# After "sad", "unhappy", "upset", "down": a preposition or "because" makes it about a thing.
FEEL_PREP = frozenset(("with", "at", "about", "over", "by", "of", "for", "from", "because",
                       "that", "when", "since", "in", "on", "to", "after", "as", "cause"))
RULES = (
    ("greeting", "warm", 0.25, "start", "greet", "rest", GL, NO_WORDS, None,
     ("good_morning|good_afternoon|good_evening",)),
    ("greeting", "warm", 0.20, "start", "greet", "rest", GL, NO_WORDS, None,
     ("hello|hi|hiya|howdy|heya|hello_there|hi_there|hey_there|morning",)),
    ("greeting", "warm", 0.45, "start", "greet", "rest", GL, NO_WORDS, None,
     ("good_to_see_you|nice_to_see_you|great_to_see_you|glad_to_see_you"
      "|it's_good_to_see_you|it's_nice_to_see_you|it's_great_to_see_you",)),
    ("goodnight", "warm", 0.15, "start", "greet", "rest", GL, NO_WORDS, None,
     ("good_night|goodnight|night_night|nighty_night|night|gnight",)),
    ("goodnight", "warm", 0.15, "start", "tight", "rest", GL, NO_WORDS, None,
     ("sleep_well|sweet_dreams",)),

    ("thanks", "thanks", 0.45, "start_or_end", "thanks", "rest", TL,
     frozenset(("but", "to", "god", "goodness", "heavens", "gosh", "christ", "anyway",
                "though", "giving", "note", "card", "letter", "email", "message",
                "speech", "notes", "cards", "mail", "page", "list", "next", "obama")),
     THANKS_BOOST,
     ("thank_you|thanks|thx|thankyou|many_thanks|much_appreciated|i_appreciate_it"
      "|i_appreciate_that|i_appreciate_you|appreciate_it",)),
    ("appreciate_help", "thanks", 0.45, "any", "tightlast", False, FILLERS, NO_WORDS, THANKS_BOOST,
     ("i? appreciate_your_help|appreciate_the_help",)),

    ("apology", "warm", 0.25, "start", "tightlast", False, FILLERS, frozenset(("but",)), None,
     ("sorry_pip|i'm_sorry_pip|pip_i'm_sorry|pip_sorry|sorry_about_that_pip"
      "|i'm_sorry_about_that_pip|my_bad_pip|pip_my_bad|i_apologize_pip|i_apologise_pip"
      "|sorry_for_that_pip",)),

    # "unhappy with the couch", "sad about the score", "sad at the price": a complaint or a
    # reaction to a thing, not a feeling to soothe. Only "lonely" and the strong words below
    # may be followed by a preposition.
    ("sad", "worry", 0.62, "start", "any", False, FILLERS, FEEL_PREP, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|i've_been|feeling @SAD",)),
    ("sad", "worry", 0.62, "start", "any", False, FILLERS, NO_WORDS, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|i've_been|feeling @LONELY",)),
    ("sad", "worry", 0.75, "start", "any", False, FILLERS, NO_WORDS, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|i've_been|feeling @SAD_STRONG",)),
    ("upset", "worry", 0.40, "start", "any", False, FILLERS,
     FEEL_PREP, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|feeling @UPSET",)),
    ("down", "worry", 0.45, "start", "any", False, FILLERS,
     FEEL_PREP | frozenset(("key", "ish")), None,
     ("i_feel|i'm_feeling|i've_been_feeling|feeling @DOWN",)),
    ("rough_day", "worry", 0.30, "start", "any", False, FILLERS,
     frozenset(("on", "with", "for")), None,
     ("i_had|i've_had|i'm_having|it's_been|today_was|today's_been|today_has_been"
      "|this_week_has_been|this_week_was|i_just_had such? a? @ROUGH @DAYN",)),
    ("rough_day", "worry", 0.30, "start", "any", False, FILLERS,
     frozenset(("on", "with", "for")), None,
     ("today_was|today's_been|today_has_been @ROUGH",)),
    # tail "vtime": only a vocative or a time word may follow, so "in minecraft", "on level
    # three", "of embarrassment", "at the mall" have nothing to stand on
    ("loss", "worry", 0.68, "any", "vtime", False, FILLERS, NO_WORDS, None,
     ("my @OLDADJ? @KIN _? has? died|passed_away",)),
    ("loss", "worry", 0.65, "start", "vtime", False, FILLERS, NO_WORDS, None,
     ("i_lost|i've_lost my @OLDADJ? @KIN",)),
    ("lost_job", "worry", 0.50, "start", "tight", False, FILLERS,
     frozenset(("up", "application", "applications", "search", "interview", "interviews",
                "form", "forms", "hunt", "offer", "board", "listing", "posting")), None,
     ("i_lost_my_job|i_got_fired|i_was_fired|i_got_laid_off|i_was_laid_off"
      "|i've_been_fired|i've_been_laid_off",)),
    ("failed", "worry", 0.45, "start", "any", False, FILLERS, NO_WORDS, None,
     ("i_failed|i_flunked my|the|a|an _? @EXAMS",
      "i_failed|i_flunked my|the|a|an @TESTKIND test")),
    ("miss", "worry", 0.50, "start", "tight", False, FILLERS, NO_WORDS, None,
     ("i_miss|we_miss my|our|the @OLDADJ? @MISSKIN",)),
    # tail "vtime": "crying at / over / about <a video, a puppy, an ending>" is amusement or
    # cuteness as often as grief, so nothing but a vocative or a time word may follow
    ("crying", "worry", 0.62, "start", "vtime", False, FILLERS,
     frozenset(("laughing", "laughter", "laugh", "with", "from")), None,
     ("i'm_crying|i've_been_crying|i_feel_like_crying|i_want_to_cry|i_just_want_to_cry",)),

    ("anxious", "support", 0.30, "start", "any", False, FILLERS, ANX_NOT_NEXT, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|feeling @ANX out?",)),
    ("anxious", "support", 0.35, "start", "any", False, FILLERS, ANX_NOT_NEXT, None,
     ("i'm|i_feel|i'm_feeling|i've_been_feeling|feeling @ANX_STRONG",)),
    ("luck", "support", 0.65, "any", "any", False, FILLERS, NO_WORDS, None,
     ("wish_me_luck|wish_me_good_luck|cross_your_fingers_for_me|keep_your_fingers_crossed",)),

    ("laugh", "humour", 0.45, "any", "tightlast", False, FILLERS, frozenset(("no", "nope", "not")), None,
     ("@LAUGH_SMALL", "ha_ha")),
    ("laugh", "humour", 0.80, "any", "tightlast", False, FILLERS, frozenset(("no", "nope", "not")), None,
     ("@LAUGH_BIG", "ha_ha_ha")),
    ("funny", "humour", 0.45, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("that's|that_was|it's|it_was|you're|you_were|this_is|this_was|that_joke_was"
      "|that_joke's @FUNNY",)),
    ("funny", "humour", 0.65, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("that's|that_was|it's|it_was|you're|you_were|this_is|this_was|that_joke_was"
      "|that_joke's @HILARIOUS",)),
    ("made_laugh", "humour", 0.60, "start", "tightlast", False, FILLERS, frozenset(("at",)), None,
     ("that_made_me_laugh|you_made_me_laugh|that_cracked_me_up|you_cracked_me_up"
      "|you_crack_me_up|i_can't_stop_laughing|i'm_laughing|i'm_dying_laughing"
      "|i_love_that_joke",)),
    ("kidding", "humour", 0.30, "any", "tightlast", False, FILLERS,
     frozenset(("myself", "yourself", "around", "ourselves", "himself", "herself")), None,
     ("just_kidding|i'm_kidding|i'm_just_kidding|i'm_joking|i_was_joking"
      "|just_joking|jk|i'm_only_joking|only_kidding|only_joking",)),
    ("knock", "humour", 0.30, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("knock_knock who's_there?",)),

    ("wow", "surprise", 0.40, "start", "tightlast", False, FILLERS | frozenset(("oh",)), NO_WORDS, None,
     ("wow|whoa|woah|whoah|wowza|oh_wow|holy_cow|holy_moly",)),
    # tail "gasp": nothing, or "really", after it. A vocative ("oh my god pip") is as often
    # exasperation as awe, so it gives no reaction at all (the lower "wow" strength was the
    # other option; None was chosen: a wrong gasp is worse than none).
    ("big_surprise", "surprise", 0.65, "start", "gasp", False, FILLERS, NO_WORDS, None,
     ("oh_my_god|oh_my_gosh|oh_my_goodness|oh_my_word|omg|good_heavens|holy_smokes"
      "|i_can't_believe_it",)),
    ("no_way", "surprise", 0.55, "start", "noway", True, FILLERS, NO_WORDS, None,
     ("no_way",)),

    ("delight", "warm", 0.50, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("that's|it's|that_was|this_is @DELIGHT",)),
    ("praise", "warm", 0.50, "start", "tightlast", False, FILLERS, NO_WORDS, PRAISE_BOOST,
     ("good_job|great_job|nice_job|well_done|good_work|great_work|nice_work|bravo"
      "|excellent_job|amazing_job|awesome_job|brilliant_job",)),
    ("praise", "warm", 0.55, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("you_did|you've_done @PRAISE_DONE",
      "you_did|you've_done a|an good|great|amazing|awesome|nice job",
      "you're the? @PRAISE_ADJ")),
    ("meet", "warm", 0.30, "start", "tightlast", False, GL, NO_WORDS, None,
     ("nice_to_meet_you|good_to_meet_you|pleased_to_meet_you|great_to_meet_you"
      "|it's_nice_to_meet_you|it's_good_to_meet_you|it's_great_to_meet_you",)),
    ("congrats", "warm", 0.55, "start", "tightlast", False, GL, NO_WORDS, None,
     ("congratulations|congrats",)),
    ("proud", "warm", 0.55, "start", "tightlast", False, GL, NO_WORDS, None,
     ("i'm|we're proud_of_you", "proud_of_you")),
    ("love", "warm", 0.75, "start", "tightlast", False, FILLERS, NO_WORDS, None,
     ("i_love_you_pip|love_you_pip|pip_i_love_you|pip_love_you|i_like_you_pip",)),
)
