"""Host tests for mind.affect: what the person's words say, for Pip's face.

Every line below is synthetic (written for this file); no real owner speech.
Evidence label for everything here: host test.

Run like the rest of the Mac-side suite:
    python3 -m unittest tests.test_affect -v
Micro-benchmark (prints microseconds per read(), labelled host):
    python3 -m tests.test_affect --bench

Third review round: the rule is now STRUCTURAL (the short-utterance rule in
mind/affect.py). read() reacts only when the whole utterance is the cue plus
filler, so the checks in ShortUtteranceRule are GENERATED: every positive, with
each of ten complaint or neutral sentences appended, and with each of four hedges
in front, must give None. A line the builder never thought of fails the same way.
"""

import json
import math
import sys
import time
import unittest
from pathlib import Path

from mind import affect

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "crates" / "brainstem" / "data" / "emotion-director.json"
CONCEPTS = ROOT / "crates" / "face" / "concept-expressions.json"
GROUPS = ROOT / "docs" / "design" / "emotion-director-groups.json"

# The shared contract: each family's ladder, rung 0 first.
LADDERS = {
    "warm": ("pleased", "joy", "excited"),
    "humour": ("amused", "giggle", "laugh"),
    "curious": ("curiosity", "idea"),
    "doubt": ("skepticism", "smug", "annoyed"),
    "worry": ("concern", "sad", "disappointed"),
    "surprise": ("surprise", "gasp", "shocked"),
    "thanks": ("pleased", "grateful"),
    "apology": ("concern", "guilty"),
    "support": ("reassuring", "encouraging"),
    "calm": ("neutral",),
}

# (text, expected family)
POSITIVES = [
    # greeting -> warm
    ("Good morning", "warm"),
    ("good morning Pip", "warm"),
    ("Good morning, Pip!", "warm"),
    ("Good afternoon.", "warm"),
    ("Good evening, Pip", "warm"),
    ("Hello", "warm"),
    ("Hello Pip", "warm"),
    ("hello there", "warm"),
    ("Hi!", "warm"),
    ("hi pip", "warm"),
    ("Well, hello again", "warm"),
    ("hello pip what time is it", "warm"),
    ("good morning, how did you sleep?", "warm"),
    ("good morning pip how are you", "warm"),
    ("Oh hello", "warm"),
    ("it's good to see you again", "warm"),
    ("Nice to see you, Pip", "warm"),
    ("hey there", "warm"),
    ("Morning, Pip", "warm"),
    ("Howdy", "warm"),
    # goodnight -> warm, mild
    ("Good night", "warm"),
    ("goodnight Pip", "warm"),
    ("Good night, sleep well", "warm"),
    ("nighty night", "warm"),
    ("Sweet dreams, Pip", "warm"),
    ("good night, see you tomorrow", "warm"),
    # thanks
    ("Thank you", "thanks"),
    ("thanks", "thanks"),
    ("Thanks, Pip!", "thanks"),
    ("thank you so much", "thanks"),
    ("Thanks a lot Pip", "thanks"),
    ("thanks so much", "thanks"),
    ("Oh, thank you", "thanks"),
    ("Great, thanks", "thanks"),
    ("Thank you for the reminder", "thanks"),
    ("thanks for your help", "thanks"),
    ("Many thanks", "thanks"),
    ("thanks for coming", "thanks"),
    ("thanks for telling me", "thanks"),
    ("thank you for listening", "thanks"),
    ("thanks for the help", "thanks"),
    ("thank you for your help Pip", "thanks"),
    ("thanks for helping me with this", "thanks"),
    ("thank you for waiting", "thanks"),
    ("I really appreciate your help", "thanks"),
    ("appreciate your help", "thanks"),
    ("thanks pip, good night", "thanks"),
    ("I really appreciate it", "thanks"),
    ("Much appreciated", "thanks"),
    ("yeah thanks pip", "thanks"),
    # apology aimed at Pip -> warm, mild
    ("Sorry Pip", "warm"),
    ("sorry, Pip", "warm"),
    ("I'm sorry Pip", "warm"),
    ("Pip, I'm sorry", "warm"),
    ("my bad Pip", "warm"),
    ("Sorry about that, Pip", "warm"),
    ("I apologize Pip", "warm"),
    # sadness and loss -> worry
    ("I'm sad", "worry"),
    ("I am sad", "worry"),
    ("I feel sad today", "worry"),
    ("I feel sad this morning", "worry"),
    ("I'm feeling really sad", "worry"),
    ("i'm so sad", "worry"),
    ("I'm a bit sad", "worry"),
    ("I'm kind of sad", "worry"),
    ("feeling sad today", "worry"),
    ("I'm unhappy", "worry"),
    ("I'm really upset about it", "worry"),
    ("I'm lonely", "worry"),
    ("I've been feeling lonely lately", "worry"),
    ("I'm heartbroken", "worry"),
    ("I'm so depressed", "worry"),
    ("I'm feeling down", "worry"),
    ("I feel low today", "worry"),
    ("I had a rough day", "worry"),
    ("I had a really bad day at work", "worry"),
    ("it's been a terrible week", "worry"),
    ("Today was awful", "worry"),
    ("I've had a tough month", "worry"),
    ("I'm having a bad day", "worry"),
    ("my dog died", "worry"),
    ("My cat passed away last night", "worry"),
    ("my old dog just died", "worry"),
    ("my grandma passed away", "worry"),
    ("my dad has died", "worry"),
    ("my dog Max died yesterday", "worry"),
    ("my cat died last night", "worry"),
    ("my sister passed away", "worry"),
    ("my best friend died", "worry"),
    ("I lost my dog", "worry"),
    ("I lost my job", "worry"),
    ("I got fired today", "worry"),
    ("I failed my exam", "worry"),
    ("I failed my driving test", "worry"),
    ("I miss my mom", "worry"),
    ("I really miss my old dog", "worry"),
    ("I'm crying", "worry"),
    ("I feel like crying", "worry"),
    # anxiety -> support
    ("I'm nervous", "support"),
    ("I'm anxious", "support"),
    ("I'm stressed out", "support"),
    ("I feel scared", "support"),
    ("wish me luck", "support"),
    ("wish me luck Pip", "support"),
    ("wish me luck on my interview tomorrow", "support"),
    ("wish me luck on the exam", "support"),
    # laughter, jokes -> humour
    ("haha", "humour"),
    ("hahaha", "humour"),
    ("hehe", "humour"),
    ("lol", "humour"),
    ("lmao", "humour"),
    ("ha ha ha", "humour"),
    ("That's funny", "humour"),
    ("that's so funny", "humour"),
    ("that was funny Pip", "humour"),
    ("You're funny", "humour"),
    ("That's hilarious", "humour"),
    ("you're hilarious, Pip", "humour"),
    ("that made me laugh", "humour"),
    ("you crack me up", "humour"),
    ("just kidding", "humour"),
    ("I'm kidding", "humour"),
    ("I was joking", "humour"),
    ("jk", "humour"),
    ("Knock knock", "humour"),
    # surprise and delight
    ("Wow", "surprise"),
    ("wow!", "surprise"),
    ("Oh wow", "surprise"),
    ("whoa", "surprise"),
    ("Wow, Pip!", "surprise"),
    ("oh my god!", "surprise"),
    ("omg", "surprise"),
    ("I can't believe it", "surprise"),
    ("no way, really?", "surprise"),
    ("That's amazing", "warm"),
    ("that's awesome Pip", "warm"),
    ("It's incredible", "warm"),
    ("that's wonderful", "warm"),
    # praise and affection
    ("Good job", "warm"),
    ("good job Pip", "warm"),
    ("Great job!", "warm"),
    ("Nice work", "warm"),
    ("Well done", "warm"),
    ("well done, Pip", "warm"),
    ("you did great", "warm"),
    ("you did really well", "warm"),
    ("you did a great job", "warm"),
    ("You're amazing", "warm"),
    ("you're the best", "warm"),
    ("You are awesome, Pip", "warm"),
    ("I love you Pip", "warm"),
    ("love you, Pip", "warm"),
    ("nice to meet you", "warm"),
    ("Nice to meet you, Pip", "warm"),
    ("Congratulations", "warm"),
    ("congrats Pip", "warm"),
    ("I'm so proud of you", "warm"),
    ("proud of you Pip", "warm"),
    # mixed lines: the caring family wins
    ("hello, I'm sad", "worry"),
    ("thanks, I had a rough day", "worry"),
    ("wow, my dog died", "worry"),
    ("good morning. I'm so nervous today", "support"),
    ("haha. thank you Pip", "thanks"),
    ("hello Pip. thank you for yesterday", "thanks"),
    ("hello pip can you set a timer", "warm"),
    ("my hamster passed away", "worry"),
    ("I miss my old dog", "worry"),
    ("oh my god pip", "surprise"),
    ("Today was rough", "worry"),
    ("knock knock who's there", "humour"),
]

# Recall given up on purpose (third review round). Each line gave a reaction before the
# short-utterance rule and the Round 3 patches; each now gives None and is pinned that way,
# so the cost stays visible and a later change cannot quietly bring one back.
# (text, what it used to give, why it is silent now)
RECALL_COST = [
    ("that was great thank you", "thanks", "an uncovered 'that was ...' before the cue"),
    ("I got it, thanks Pip", "thanks", "something other than filler before the cue"),
    ("Thanks! What's next?", "thanks", "a second sentence with no cue in it"),
    ("thank you, can you also set a timer?", "thanks", "a clause with no cue that is not a question opener"),
    ("I'm so nervous about tomorrow", "support", "anxious: 'about' is not allowed next"),
    ("I'm worried about my exam", "support", "anxious: 'about' is not allowed next"),
    ("I'm feeling overwhelmed", "support", "'overwhelmed' dropped from the anxious words"),
    ("Haha, that was good", "humour", "laugh must end the sentence; 'that was ...' is uncovered"),
    ("No way!", "surprise", "a bare 'no way!' is a refusal as often as a gasp"),
    ("no way you did that", "surprise", "an uncovered stretch opening with 'you'"),
    ("I'm kidding, my dog is fine", "humour", "kidding must end the sentence"),
    ("I'm feeling lonely and that is fine", "worry", "a stretch opening with 'and'"),
    ("my dad died in a car crash and I am angry", "worry", "a stretch opening with 'in', then more than the cue"),
]

# Every rule's allowed poses (the ladders of crates/brainstem/data/emotion-director.json).
RULE_POSES = {
    "greeting": {"pleased"},
    "goodnight": {"pleased"},
    "apology": {"pleased"},
    "thanks": {"pleased", "grateful"},
    "sad": {"concern", "sad", "disappointed"},
    "upset": {"concern"},
    "down": {"concern"},
    "rough_day": {"concern"},
    "loss": {"sad"},
    "lost_job": {"concern"},
    "failed": {"concern"},
    "miss": {"concern", "sad"},
    "crying": {"sad"},
    "anxious": {"reassuring"},
    "luck": {"encouraging"},
    "laugh": {"amused", "laugh"},
    "funny": {"amused", "giggle"},
    "made_laugh": {"giggle"},
    "kidding": {"amused"},
    "knock": {"amused"},
    "wow": {"surprise"},
    "big_surprise": {"gasp"},
    "no_way": {"surprise"},
    "delight": {"pleased"},
    "praise": {"pleased", "joy"},
    "love": {"joy"},
    "appreciate_help": {"pleased", "grateful"},
    "meet": {"pleased"},
    "congrats": {"pleased"},
    "proud": {"pleased", "joy"},
}

# (text, the pose the brainstem's ladder gives it)
RUNG_PINS = [
    ("I'm sad", "sad"),
    ("I'm a bit sad", "concern"),
    ("I'm so depressed", "disappointed"),
    ("I'm really upset about it", "concern"),
    ("I had a rough day", "concern"),
    ("my dog died", "sad"),
    ("I lost my job", "concern"),
    ("I'm crying", "sad"),
    ("I'm nervous", "reassuring"),
    ("wish me luck", "encouraging"),
    ("thanks", "pleased"),
    ("thank you so much", "grateful"),
    ("hello", "pleased"),
    ("good night", "pleased"),
    ("Sorry Pip", "pleased"),
    ("haha", "amused"),
    ("hahaha", "laugh"),
    ("that made me laugh", "giggle"),
    ("just kidding", "amused"),
    ("wow", "surprise"),
    ("oh my god", "gasp"),
    ("no way, really?", "surprise"),
    ("That's amazing", "pleased"),
    ("good job", "pleased"),
    ("Great job!", "joy"),
    ("I love you Pip", "joy"),
]

# Lines that must give None.
NEGATIVES = [
    # negation
    "I'm not sad",
    "I am not sad at all",
    "I'm never sad around you",
    "I'm no longer sad",
    "I don't feel sad",
    "I'm not upset",
    "I'm not nervous",
    "I'm not worried about it",
    "that's not funny",
    "that was not funny at all",
    "you're not funny",
    "no thanks",
    "no thank you",
    "not thanks",
    "I'm not crying",
    "my dog didn't die",
    "my cat did not die",
    "nobody died",
    "I never said I was sad",
    "it wasn't a rough day",
    # questions
    "why are you sad?",
    "are you sad?",
    "are you sad",
    "why are you so sad",
    "what is a good morning routine?",
    "do you say thank you when someone helps you?",
    "did I thank you?",
    "should I say thanks?",
    "is that funny?",
    "can you wish me luck?",
    "how was your day",
    "how are you?",
    "hello?",
    "hello? can you hear me?",
    "am I sad?",
    "I'm sad?",
    "what makes a dog die?",
    "do you know any jokes?",
    "is it a good job?",
    "have you ever lost a job?",
    # hypothetical, reported speech, quotes
    "if I'm sad you will notice",
    "when I'm sad I eat toast",
    "imagine my dog died",
    "pretend I'm sad",
    "what if I lose my job",
    "she said I'm sad",
    "he told me to say thank you to you",
    "remind me to say thank you to the neighbour",
    "write a thank you note for my aunt",
    "I need a thank you card",
    "my friend said his phone died",
    "I said \"I'm sad\" in the play",
    "the character says \"thank you\" at the end",
    "my brother's dog died years ago in the story",
    "hello world is the first program everyone writes",
    "tell me a joke",
    "play something sad",
    "play a sad song",
    "make me laugh",
    "tell me something funny",
    "read me a funny story",
    "set a reminder to thank my teacher",
    # sarcasm
    "thanks for nothing",
    "oh great, thanks",
    "yeah right",
    "oh wonderful",
    "oh very funny",
    "ha ha, very funny",
    "gee thanks",
    "thanks for that",
    "thanks genius",
    "good job genius",
    "wow, thanks a lot",
    "wow great",
    "oh thanks a lot",
    "thanks but no thanks",
    "thanks anyway",
    "yeah sure, thanks",
    "as if I'm sad",
    "nice going",
    "oh good job",
    # sensory and body claims (never mapped)
    "I see",
    "I hear you",
    "I hear a noise outside",
    "I looked at the screen",
    "I feel hot",
    "I feel cold",
    "I'm tired",
    "I'm sleepy",
    "I'm hungry",
    "I feel sick",
    "I'm so hot today",
    "it's cold in here",
    "it's loud in here",
    "I can see the light",
    "I can't see anything",
    "my head hurts",
    "I'm thirsty",
    "I feel dizzy",
    "I'm exhausted",
    "I heard that",
    # neutral facts and statements
    "the meeting is at three",
    "I have a dentist appointment on Tuesday",
    "my sister is visiting next week",
    "I bought a new lamp",
    "the weather is nice today",
    "my dog is hungry",
    "my dog is asleep",
    "my phone died",
    "my battery died",
    "the plant died",
    "my laptop crashed",
    "I'm going to the shop",
    "I'm home",
    "I'm back",
    "I'm ready",
    "I'm fine",
    "I am here",
    "I'm okay",
    "I'm good",
    "I'm busy",
    "I'm a teacher",
    "I'm a sad clown",
    "my dad is a funny man",
    "I lost my keys",
    "I lost my phone",
    "I lost my balance",
    "I miss the bus",
    "I miss my train",
    "I failed to find it",
    "it's a funny word",
    "that's funny, my keys were in the fridge",
    "it's funny how the clock runs slow",
    "that's a good job for a carpenter",
    "good mornings are my favourite",
    "I had a long day",
    "I had a good day",
    "I had a good night's sleep",
    "he had a rough day",
    "she is sad",
    "you are sad",
    "they're upset about it",
    "the movie was sad",
    "it's a sad story",
    "the tomatoes were nice",
    "I'm down for pizza",
    "I'm down to my last battery",
    "no way I'm doing that",
    "no way to know",
    "no way",
    "there is no way home",
    "oh no",
    "oh my",
    "I'm afraid I can't",
    "I'm sorry to hear that",
    "sorry what did you say",
    "sorry",
    "excuse me",
    "my bad",
    # commands and requests
    "what time is it",
    "what's the weather",
    "set a timer for ten minutes",
    "turn the volume down",
    "stop",
    "cancel that",
    "remind me to call the dentist",
    "open the calendar",
    "add milk to the shopping list",
    "how many days until Friday",
    "read my notes",
    "go to sleep",
    "be quiet",
    "say hello to my mother",
    "tell my brother good morning",
    "say good night to the kids",
    "what does thank you mean in French",
    "translate good morning into Spanish",
    "play some music",
    "hey Pip what is the date",
    "hey",
    "um",
    "yes",
    "okay",
    "no",
    # near misses found while tuning (kept as regression pins)
    "my dog died in the movie",
    "my friend died in the game",
    "my mom died laughing",
    "my phone's battery died",
    "hello is anyone there",
    "hello is this thing on",
    "hello kitty",
    "hi fi system",
    "good morning america",
    "sweet dreams are made of this",
    "that's amazing, you broke it",
    "thanks a lot, now it's broken",
    "that's not what I wanted thanks",
    "I'm nervous but excited",
    "thank goodness",
    "thank god it's Friday",
    "thanks to the rain I'm late",
    "no way pip",
    "no way jose",
    "good job breaking it",
    "nice job ruining my afternoon",
    "you're amazing at math",
    "well done steak please",
    "haha no",
    "I wish my dog didn't die",
    "my dog is dying to go out",
    "I'm sorry pip but no",
    "that's funny because it's true",
    "oh my god you are so slow",
    "I can't believe you did that",
    # review findings: thanks then a complaint, wow with a tail, said-X sarcasm, apology tail,
    # home, upset, laugh at an insult, thanks a lot after worry (adversarial review)
    "thank you for the update, but that was wrong",
    "thanks, that is not what I asked",
    "wow that is a terrible idea",
    "wow, I am so angry right now",
    "Wow, you broke it",
    "I love you pip, said no one ever",
    "you did great, said nobody",
    "sorry pip, I'm late for work",
    "sorry, pip is the name of my cat",
    "I'm sorry pip, you're wrong",
    "I miss the old home page design",
    "I miss my dog's barking, it was so annoying",
    "I'm so upset at you pip",
    "i'm upset with you",
    "I'm upset you did that",
    "lol you are so wrong",
    "I'm anxious, thanks a lot",
    "I'm nervous about you",
    "i'm scared of you",
    "wow that is a lot of snow",
    # adversarial review lines, silent on purpose (negation, questions, reported, fiction, body, bounds)
    "I am not feeling sad at all",
    "don't be sad",
    "Why am I so sad?",
    "my friend said I'm sad",
    "she told me thanks",
    "If I were sad I would tell you",
    "I was sad yesterday but I'm fine now",
    "I'm not unhappy",
    "no, thanks",
    "thank you, now stop talking",
    "I'd like to thank the academy",
    "thanks to the new model it crashes",
    "I see you",
    "I'm sick",
    "I'm so sleepy pip",
    "I feel sick today",
    "if my cat died I would cry",
    "my goldfish died",
    "hello world program in python",
    "hi, I need you to shut down",
    "hi tech companies are hiring",
    "morning routine tips please",
    "haha not funny",
    "it's not funny",
    "that's funny but also sad",
    "omg shut up",
    "great job pip, you broke it",
    "good job, genius",
    "nice work breaking everything",
    "i'm sad lol",
    "sorry pip but you are wrong",
    "my grandma, who I live with, is sick",
    "my dad and mom died",
    "I'm worried you will break",
    "I'm terrified of what you said",
    "I'm stressed because of you",
    "wish me luck on killing the process",
    "I failed the test lol",
    "we miss the bus",
    # own adversarial lines: accusation, insult, condition or blame next to a reaction word
    "thank you for nothing pip",
    "thanks, that did not help",
    "thanks pip, but I already knew that",
    "thank you, although it took forever",
    "thanks, still wrong",
    "good morning, you woke me up too early",
    "good morning and I hate mondays",
    "hello, you are not working again",
    "hi, why are you so slow",
    "wow, you are so slow today",
    "wow you really messed that up",
    "wow pip that was a mistake",
    "haha you got it wrong again",
    "lol that was a bad answer",
    "hahaha you are useless",
    "that's amazing, said nobody",
    "I'm kidding, you are terrible",
    "you did great, not",
    "you did great at ruining my day",
    "you're the best at getting it wrong",
    "good job pip, you deleted my file",
    "well done, you broke the timer",
    "I love you pip, now shut up",
    "I love you pip, not",
    "love you pip, but you are annoying",
    "I love pizza",
    "I love you all, said the host",
    "sorry pip, you need to be quiet",
    "sorry pip, my cat is on the keyboard",
    "I'm sorry pip, I did not mean to say that to the dog",
    "pip, sorry is not enough",
    "my bad pip, the real problem is you",
    "I'm sad that you cannot do it",
    "I'm upset about what you said",
    "I'm so upset, you ruined it",
    "I'm scared you will crash",
    "I'm worried about you pip",
    "I'm nervous when you talk",
    "I'm so nervous about you listening",
    "I'm overwhelmed by your answers",
    "wish me luck killing the process",
    "wish me luck deleting all of it",
    "I miss the old firmware",
    "I miss the old home screen layout",
    "I miss the bus every day",
    "I miss my flight",
    "I failed to understand the test question",
    "my dad died laughing at the show",
    "my dog died in the video game",
    "my cat's toy died",
    "my phone died, my grandma passed me the charger",
    "my friend's dog died in the book I read",
    "I lost my mom's recipe",
    "she lost her job",
    "he failed his exam",
    "you lost my file",
    "I miss you pip",
    "I love you pip, but you keep getting my name wrong",
    "I love you pip, said the robot in the movie",
    "i like you pip but not your voice",
    "love you pip, not really",
    "oh my god pip you are useless",
    "omg that is so annoying",
    "omg my phone is broken",
    "oh my god, I'm so angry",
    "i can't believe it is already friday",
    "I can't believe it, you got it wrong again",
    "no way, that is wrong",
    "no way, you did not just say that",
    "that's wonderful, if it were true",
    "that's incredible, I guess",
    "it's awesome that you broke my timer",
    "that's amazing news, I lost the game though",
    "that's fantastic, not",
    "you're amazing, as always, at ignoring me",
    "you're the best, when you work",
    "you did great, if you ignore the mistakes",
    "you did well for a toaster",
    "good job, pip, but that was the wrong song",
    "nice work, now it is worse",
    "bravo, you failed again",
    "hilarious, you got it wrong",
    "that's hilarious, you crashed",
    "that's funny, you forgot my name",
    "you're funny, pip, but I'm still mad",
    "that made me laugh, then cry",
    "you crack me up, not",
    "that joke was not funny",
    "i'm laughing at your mistake",
    "I'm crying because of you",
    "I'm crying from the onions",
    "I lost my job to a robot like you",
    "I failed my test because of you",
    "I'm so down on this brand",
    "I'm feeling down about your answers",
    "I feel low on battery",
    "today was rough on the keyboard",
    "I had a bad day at the gym, said no one",
    "I'm nervous, you better not mess up",
    "I'm worried, you are lying",
    "I'm stressed out by the noise you make",
    "hello pip you are late",
    "hello, are you broken",
    "hi pip stop talking",
    "good night, and thanks for nothing",
    "good night, don't wake me",
    "sweet dreams, if you could dream",
    "sleep well, said no one",
    "thank you pip for being so slow",
    # third review round: every line of the reviewers' scratch files that should be silent
    "I'm sad but also happy",
    "i\u2019m s\u0430d",
    "\"I'm sad\"",
    "He said 'I'm sad'",
    "I'm sad. Just kidding",
    "I'm anxious, thank you so much",
    "I'm stressed, thanks so much for that",
    "I'm feeling sad about the football score",
    "no way you are right about this one",
    "jk you are the worst",
    "knock knock, who is stopping the music",
    "I got fired, you said it would be fine",
    "wish me luck, you will need it",
    "goodnight pip, the neighbours are loud",
    "thanks, bye",
    "thanks for waking up the baby",
    "thanks for telling everyone my secret",
    "thanks for scaring me half to death",
    "thanks for losing my homework",
    "nice job, now the lights are all off",
    "well done pip, the soup is cold now",
    "good job. you deleted my alarm",
    "Thank you. That was not what I asked.",
    "wow. that is a terrible idea.",
    "I love you pip. not.",
    "thanks obama",
    "thank you, next",
    "thanks a bunch",
    "my sister says she's so sad today",
    "my son is crying again",
    "my mom passed on the leftovers",
    "my friend passed on the job offer",
    "I got fired up about the game tonight",
    "I was fired up all day for the match",
    "I lost my job application form",
    "I failed the unit test again, fixing it now",
    "I failed the integration test run",
    "I'm overwhelmed with joy",
    "I'm anxious to see the results",
    "I'm scared of nothing",
    "I'm crying, I'm cutting onions",
    "I'm sad. Not.",
    "I was kidding myself, I'm so sad",
    "I'm sad, said Eeyore",
    "I feel blue about the colour of the walls",
    "I miss the dog park",
    "I miss the friends reunion episode",
    "oh my god, please stop",
    "omg, you talk too much",
    "no way, put that down!",
    "no way!",
    "wow. you are so slow.",
    "I can't believe it, you talk too much",
    "haha, you wish",
    "lmao stop",
    "good morning, says the clock",
    "hello, I'm calling about my invoice",
    "hi-fi speakers are on sale",
    "today was a bad day for the stock market",
    "it's been a rough week for the whole office",
    "I'm feeling low-key excited",
    "I'm feeling low key hungry",
    "play Thank You for the Music",
    "hello darkness my old friend",
    "thank you for calling customer support",
    "thanks to you I am in trouble",
    "I appreciate it when you stay quiet",
    "you're the best at nothing",
    "that's awesome, said nobody",
    "good job pip, good job, you broke the toaster",
    "I'm sorry pip but the answer was wrong",
    "I feel sick of this homework",
    "my kid says I'm lonely",
    "I'm stressed out of my mind with excitement",
    "I'm so nervous and excited",
    "sorry pip I was short with you",
    # third review round, own attack lines: junk glued to a cue, STT style with no punctuation
    "thank you that was not what i asked",
    "thanks pip it still does not work",
    "thank you pip i guess",
    "good job pip it is still broken",
    "good morning pip it is raining again",
    "hello pip you are late again",
    "hi pip where have you been",
    "good night pip and do not wake me",
    "good night pip i hate you",
    "wow pip you are slow",
    "wow nice try",
    "wow i did not expect that to fail",
    "lol pip you are broken",
    "haha pip no",
    "haha that was not funny",
    "that was funny but also rude",
    "that's funny pip not",
    "you're funny pip not",
    "i love you pip not",
    "i love you pip lol jk",
    "i love you pip said the villain",
    "sorry pip i did not mean it",
    "sorry pip for the mess",
    "i'm sad about you",
    "i'm sad you left",
    "i'm sad because of you",
    "i'm so lonely without you",
    "i'm worried about you",
    "my dog died because of you",
    "my dog died and you did nothing",
    "my grandma passed away because you were slow",
    "i lost my job because of you",
    "i got fired because of you",
    "i failed my exam because of you",
    "i miss my dog but not you",
    "i miss my dog said nobody",
    "thanks to you my dog died",
    "oh my god pip not again",
    "oh my god i hate this",
    "no way pip that is wrong",
    "no way you are good",
    "wish me luck pip you will need it",
    "i feel sad pip because you broke",
    "i feel sad lol",
    "thank you thank you thank you",
    "thanks thanks thanks thanks thanks thanks thanks",
    "you did great pip not",
    "you did great pip right",
    "you did great pip thanks for nothing",
    "nice work pip said nobody",
    "good job pip you finally got it right",
    "well done pip finally",
    "well done pip about time",
    "thanks for finally listening",
    "thank you for finally helping",
    "thanks for the help pip not",
    "thanks for the help pip lol",
    "thanks for the help you jerk",
    "thanks for the update pip it was wrong",
    "thanks pip you are the best not",
    "thanks pip ugh",
    "thanks pip sigh",
    "thanks pip whatever",
    "thanks pip sure",
    "hello pip hello pip hello pip",
    "hello pip are you broken",
    "hello pip did you break it",
    "hello pip can you shut up",
    "hello pip what is wrong with you",
    "good morning pip what is wrong with you",
    "good morning how did you break it",
    "good morning how could you",
    "good night pip how could you",
    "sweet dreams pip you are annoying",
    "i am sad pip you are annoying",
    "i'm sad pip shut up",
    "i had a bad day pip because of you",
    "i had a bad day thanks to you",
    "i had a rough day at work pip and you did not help",
    "my cat died last week pip you were not there",
    "i feel lonely at work because of you",
    "i feel lonely with you",
    "i feel down about you",
    "i feel down at you",
    "i feel low about you",
    "i'm crying at your jokes",
    "i'm crying with laughter",
    "i'm crying about your answers",
    "i'm crying because you are slow",
    "i'm so upset at this",
    "i'm upset with the delay",
    "i'm anxious about you",
    "i'm nervous around you",
    "i'm worried for you",
    "wish me luck pip i will need it",
    "wish me luck pip i guess",
    "wish me luck breaking it",
    "wish me luck lol",
    "knock knock go away",
    "knock knock who is it",
    "knock knock who's there not you",
    "just kidding pip you are terrible",
    "just kidding you are great",
    "jk not",
    "i'm kidding not",
    "lmao pip you are useless",
    "lmao no",
    "lol no",
    "lol ok",
    "lol sure",
    "lol right",
    "lol i guess",
    "haha ok sure",
    "ha ha ha no",
    # edge forms
    "",
    "   ",
    "...",
    "?!",
    "12345",
    "a",
    "I'm",
    "good",
    "thank",
    "my dog",
]


class Contract(unittest.TestCase):
    def test_the_ten_families_of_the_contract(self):
        self.assertEqual(
            affect.FAMILIES,
            ("warm", "humour", "curious", "doubt", "worry",
             "surprise", "thanks", "apology", "support", "calm"),
        )
        self.assertEqual(set(affect.FAMILIES), set(LADDERS))

    def test_a_cue_is_family_strength_rule(self):
        cue = affect.read("thank you")
        self.assertIsInstance(cue, affect.Cue)
        self.assertEqual(cue._fields, ("family", "strength", "rule"))
        self.assertEqual(cue.rule, "thanks")

    def test_family_and_strength_are_always_in_range(self):
        for text, _ in POSITIVES:
            cue = affect.read(text)
            self.assertIsNotNone(cue, text)
            self.assertIn(cue.family, affect.FAMILIES)
            self.assertTrue(0.0 <= cue.strength <= 1.0, (text, cue))
            self.assertTrue(cue.rule and isinstance(cue.rule, str), (text, cue))

    def test_only_social_families_are_ever_emitted(self):
        seen = {affect.read(t).family for t, _ in POSITIVES}
        self.assertTrue(seen <= {"warm", "humour", "worry", "support", "surprise", "thanks"}, seen)

    def test_an_input_that_is_not_text_gives_none(self):
        for bad in (None, 5, 3.5, b"thank you", ["thank you"], {"a": 1}):
            self.assertIsNone(affect.read(bad))


class EmotionFields(unittest.TestCase):
    def test_shape_for_the_mind_emotion_topic(self):
        fields = affect.emotion_fields(affect.Cue("thanks", 0.756, "thanks"))
        self.assertEqual(set(fields), {"family", "strength", "hold"})
        self.assertNotIn("name", fields)
        self.assertEqual(fields["family"], "thanks")
        self.assertEqual(fields["strength"], 0.76)
        self.assertEqual(fields["hold"], 1.2)

    def test_hold_is_clamped_to_the_wire_range(self):
        cue = affect.Cue("warm", 0.3, "greeting")
        self.assertEqual(affect.emotion_fields(cue, hold=10)["hold"], 2.5)
        self.assertEqual(affect.emotion_fields(cue, hold=0.01)["hold"], 0.5)
        self.assertEqual(affect.emotion_fields(cue, hold=1.0)["hold"], 1.0)
        self.assertEqual(affect.emotion_fields(cue, hold=float("nan"))["hold"], 1.2)
        self.assertEqual(affect.emotion_fields(cue, hold="soon")["hold"], 1.2)

    def test_strength_is_clamped_and_finite(self):
        self.assertEqual(affect.emotion_fields(affect.Cue("warm", 7.0, "x"))["strength"], 1.0)
        self.assertEqual(affect.emotion_fields(affect.Cue("warm", -1.0, "x"))["strength"], 0.0)
        self.assertEqual(affect.emotion_fields(affect.Cue("warm", float("inf"), "x")), {})
        self.assertEqual(affect.emotion_fields(affect.Cue("warm", float("nan"), "x")), {})

    def test_no_cue_or_an_unknown_family_gives_an_empty_dict(self):
        self.assertEqual(affect.emotion_fields(None), {})
        self.assertEqual(affect.emotion_fields(affect.Cue("sleepy", 0.5, "x")), {})
        self.assertEqual(affect.emotion_fields(("warm", 0.5, "x")), {})

    def test_fields_survive_a_json_round_trip(self):
        fields = affect.emotion_fields(affect.read("my dog died"))
        self.assertEqual(json.loads(json.dumps(fields)), fields)


class LabelledLines(unittest.TestCase):
    def test_at_least_150_labelled_lines(self):
        self.assertGreaterEqual(len(POSITIVES) + len(NEGATIVES), 150)
        self.assertGreaterEqual(len(POSITIVES), 100)
        self.assertGreaterEqual(len(NEGATIVES), 100)

    def test_positives_give_their_expected_family(self):
        wrong = []
        for text, family in POSITIVES:
            cue = affect.read(text)
            if cue is None or cue.family != family:
                wrong.append((text, family, cue))
        self.assertEqual(wrong, [], "\n".join(map(str, wrong)))

    def test_precision_floor_zero_false_positives_on_the_negatives(self):
        false_positives = [(t, affect.read(t)) for t in NEGATIVES if affect.read(t) is not None]
        self.assertEqual(false_positives, [], "\n".join(map(str, false_positives)))

    def test_labelled_lines_are_unique(self):
        texts = [t for t, _ in POSITIVES] + NEGATIVES
        dupes = {t for t in texts if texts.count(t) > 1}
        self.assertEqual(dupes, set())

    def test_every_rule_is_exercised_by_a_positive(self):
        used = {affect.read(t).rule for t, _ in POSITIVES}
        defined = {r[0] for r in affect_lexicon_rules()}
        self.assertEqual(defined - used, set(), "rules with no positive test")


def affect_lexicon_rules():
    from mind import affect_lexicon
    return affect_lexicon.RULES


class Precision(unittest.TestCase):
    def test_reply_style_claims_never_raise_a_family(self):
        # Pip's own reply words are not read here, but if the same words reach
        # read() they must still map to nothing: no sense or body claim.
        for text in ("I see.", "I hear you.", "I looked and it's there.", "I feel hot.",
                     "I'm tired.", "I can hear that.", "I see what you mean.",
                     "Let me look.", "I feel a bit warm.", "It's sleepy time."):
            self.assertIsNone(affect.read(text), text)

    def test_sleepy_is_never_an_output(self):
        for text in ("good night", "sleep well", "nighty night", "sweet dreams pip",
                     "I'm sleepy", "go to sleep", "goodnight pip"):
            cue = affect.read(text)
            self.assertTrue(cue is None or cue.family != "sleepy")
            self.assertTrue(cue is None or cue.family in affect.FAMILIES)

    def test_goodnight_is_mild_warmth_not_sleep(self):
        cue = affect.read("good night Pip")
        self.assertEqual((cue.family, cue.rule), ("warm", "goodnight"))
        self.assertLessEqual(cue.strength, 0.3)

    def test_text_never_makes_a_negative_family_from_praise_or_thanks(self):
        for text in ("thank you", "good job", "you're amazing", "that's funny", "hello"):
            self.assertNotIn(affect.read(text).family, ("worry", "doubt", "apology"))

    def test_no_line_ever_gives_doubt_curious_apology_or_calm(self):
        for text, _ in POSITIVES:
            self.assertNotIn(affect.read(text).family, ("doubt", "curious", "apology", "calm"))

    def test_strength_orders_mild_below_strong(self):
        self.assertLess(affect.read("hello").strength, affect.read("good job").strength)
        self.assertLess(affect.read("thanks").strength, affect.read("thank you so much").strength)
        self.assertLess(affect.read("I had a rough day").strength, affect.read("my dog died").strength)
        self.assertLess(affect.read("I'm kind of sad").strength, affect.read("I'm so sad").strength)
        self.assertLess(affect.read("haha").strength, affect.read("hahaha").strength)

    def test_loss_and_sadness_reach_the_sad_rung(self):
        # the table's worry ladder: concern 0, sad from 0.6
        self.assertGreaterEqual(affect.read("my dog died").strength, 0.6)
        self.assertGreaterEqual(affect.read("I'm sad").strength, 0.6)
        self.assertLess(affect.read("I had a rough day").strength, 0.6)

    def test_surprise_never_fires_on_a_bare_refusal(self):
        for text in ("no way", "No way I'm doing that", "no way to know", "No way!", "no way!",
                     "no way, put that down!", "no way, you did not just say that"):
            self.assertIsNone(affect.read(text), text)
        # astonishment needs the follow-on: "no way, really?" / "no way seriously"
        for text in ("no way, really?", "no way really", "no way seriously"):
            self.assertEqual(affect.read(text).family, "surprise", text)
        # "no way that's true" starts an uncovered stretch with "that's": silent (recall cost)
        self.assertIsNone(affect.read("no way that's true"))

    def test_a_joke_marker_never_overrides_a_worry_or_a_support_cue(self):
        # Round 3: "just kidding" retracts the sorrow; it must not turn it into a laugh,
        # and the sorrow is not read either.
        for text in ("my dog died, just kidding", "I'm sad. Just kidding",
                     "I'm so nervous, just kidding", "I was kidding myself, I'm so sad"):
            self.assertIsNone(affect.read(text), text)
        cue = affect.read("just kidding")
        self.assertEqual((cue.family, cue.rule), ("humour", "kidding"))

    def test_sorrow_and_laughter_in_one_sentence_say_nothing(self):
        self.assertIsNone(affect.read("I'm so sad haha"))
        self.assertIsNone(affect.read("I'm crying laughing"))
        self.assertIsNone(affect.read("I'm crying, that's so funny"))

    def test_curly_quotes_case_and_punctuation_are_normalised(self):
        self.assertEqual(affect.read("I’m sad").family, "worry")
        self.assertEqual(affect.read("I’M SAD").family, "worry")
        self.assertEqual(affect.read("  i'm   sad!!!  ").family, "worry")
        self.assertEqual(affect.read("THANK YOU...").family, "thanks")
        self.assertEqual(affect.read("Thank-you").family, "thanks")
        self.assertEqual(affect.read("thankyou pip").family, "thanks")
        self.assertIsNone(affect.read("I’m not sad"))
        self.assertIsNone(affect.read("“I’m sad”"))

    def test_im_without_an_apostrophe_still_reads(self):
        self.assertEqual(affect.read("im sad").family, "worry")
        self.assertEqual(affect.read("i am sad").family, "worry")

    def test_a_vocative_with_commas_is_one_clause(self):
        self.assertEqual(affect.read("Pip, thank you").family, "thanks")
        self.assertEqual(affect.read("Sorry, Pip").family, "warm")
        self.assertEqual(affect.read("Pip, I'm sorry").family, "warm")

    def test_apology_needs_pip(self):
        self.assertIsNone(affect.read("sorry I'm late"))
        self.assertIsNone(affect.read("I'm sorry about that"))
        self.assertIsNone(affect.read("sorry to hear that Pip"))
        self.assertIsNone(affect.read("I'm sorry Pip but you are wrong"))


class ReviewFixes(unittest.TestCase):
    """One test per finding of the adversarial review (each line is synthetic)."""

    def test_thanks_then_a_complaint_is_silent(self):
        for text in ("thank you for the update, but that was wrong",
                     "thanks, that is not what I asked",
                     "thanks pip however it still fails"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("thanks for the update").family, "thanks")

    def test_wow_reacts_only_when_nothing_follows(self):
        for text in ("wow that is a terrible idea", "wow, I am so angry right now",
                     "Wow, you broke it", "wow that is a lot of snow"):
            self.assertIsNone(affect.read(text), text)
        for text in ("wow", "Wow!", "oh wow", "Wow, Pip!"):
            self.assertEqual(affect.read(text).family, "surprise", text)

    def test_a_said_x_tag_is_sarcasm(self):
        for text in ("I love you pip, said no one ever", "you did great, said nobody",
                     "that was funny, nobody ever", "thanks pip said no one"):
            self.assertIsNone(affect.read(text), text)

    def test_an_apology_needs_a_tight_tail_and_a_bare_pip_to_merge(self):
        for text in ("sorry pip, I'm late for work", "sorry, pip is the name of my cat",
                     "I'm sorry pip, you're wrong", "sorry pip I was short with you"):
            self.assertIsNone(affect.read(text), text)
        for text in ("sorry pip", "Sorry, Pip", "Pip, I'm sorry", "my bad, Pip!",
                     "sorry about that, Pip"):
            self.assertEqual(affect.read(text).rule, "apology", text)

    def test_a_vocative_clause_merges_only_when_it_is_exactly_pip(self):
        from mind.affect import _merge_vocative
        self.assertEqual(_merge_vocative([["sorry"], ["pip"]]), [["sorry", "pip"]])
        self.assertEqual(_merge_vocative([["pip"], ["i'm", "sad"]]), [["pip", "i'm", "sad"]])
        self.assertEqual(_merge_vocative([["sorry"], ["pip", "is", "my", "cat"]]),
                         [["sorry"], ["pip", "is", "my", "cat"]])

    def test_home_is_not_something_a_person_misses(self):
        self.assertIsNone(affect.read("I miss the old home page design"))
        self.assertEqual(affect.read("I miss my mom").family, "worry")

    def test_upset_is_mild_and_never_at_pip(self):
        for text in ("I'm so upset at you pip", "i'm upset with you", "I'm upset you did that",
                     "I'm upset because of you"):
            self.assertIsNone(affect.read(text), text)
        cue = affect.read("I'm so upset about it")
        self.assertEqual(cue.family, "worry")
        self.assertLess(cue.strength, 0.6)      # concern, never the sad pose

    def test_laughter_at_an_insult_or_a_negation_is_silent(self):
        for text in ("lol you are so wrong", "haha not funny", "lol that was a bad answer"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("lol").family, "humour")
        self.assertEqual(affect.read("haha").family, "humour")
        self.assertIsNone(affect.read("Haha, that was good"))

    def test_thanks_a_lot_after_worry_is_irony(self):
        self.assertIsNone(affect.read("I'm anxious, thanks a lot"))
        # a separate sentence is read on its own: sorrow still wins over thanks
        self.assertEqual(affect.read("I'm sad. Thanks a lot").family, "worry")
        self.assertEqual(affect.read("thanks a lot").family, "thanks")
        self.assertEqual(affect.read("thanks, I had a rough day").family, "worry")

    def test_fear_aimed_at_pip_is_not_a_worry_to_soothe(self):
        for text in ("I'm nervous about you", "i'm scared of you", "I'm worried you will break"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("I'm scared pip").family, "support")

    def test_a_greeting_next_to_an_accusation_is_silent(self):
        for text in ("good morning, you woke me up too early", "hello pip you are late",
                     "hi pip stop talking", "hello, why are you so slow"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("hello pip can you set a timer").family, "warm")

    def test_a_worry_next_to_an_accusation_is_silent(self):
        self.assertIsNone(affect.read("I'm so upset, you ruined it"))
        self.assertIsNone(affect.read("I'm worried, you are lying"))
        # a clause after the cue that is not a question opener is something else being said
        self.assertIsNone(affect.read("I'm sad, you're the only one I can tell"))
        self.assertEqual(affect.read("I'm sad, how about you").rule, "sad")

    def test_down_and_rough_day_do_not_read_a_device_or_a_tool(self):
        self.assertIsNone(affect.read("I feel low on battery"))
        self.assertIsNone(affect.read("today was rough on the keyboard"))
        self.assertEqual(affect.read("I feel low today").family, "worry")


# Ten complaint or neutral sentences. None holds a cue. Several open with a word no list
# of vetoes needs to know ("My cat", "Dinner", "Lights"): the whole-utterance rule, not a
# word list, is what silences them.
CONTINUATIONS = [
    "That was not what I asked.",
    "Now fix it.",
    "You always do this.",
    "The meeting starts at nine.",
    "My cat is on the keyboard.",
    "It is cold in here.",
    "Stop talking.",
    "Why is it so slow?",
    "I need to leave soon.",
    "Dinner is at eight.",
]
HEDGES = ("if", "not", "never", "why would I say")
BEFORE = ("That was wrong.", "Nobody asked you.", "The soup is cold.", "Stop it.", "Lights are off.")
# Short tails that turn the line, glued on with every kind of joint.
TURNING = ("that was wrong", "now fix it", "you always do this", "the soup is cold",
           "it never works", "not really", "but I hate it", "nobody cares", "then it broke",
           "if you say so", "stop it", "never mind", "that is not what I asked")
# Short junk in front of a cue: nothing but filler may come before it.
JUNK_BEFORE = TURNING + ("lights are off", "dinner is late", "obama", "gross",
                         "my cat is on the keyboard", "bye")


def _with_sentence(text, sentence):
    return text + ("" if text[-1] in ".!?" else ".") + " " + sentence


class ShortUtteranceRule(unittest.TestCase):
    """GENERATED checks for the structural rule (third review round).

    Two review rounds found that patching the exact reported line leaves its siblings
    wrong. These checks take every labelled positive and break it in ways nobody listed.
    """

    def positives(self):
        texts = [t for t, _ in POSITIVES]
        for text in texts:
            self.assertIsNotNone(affect.read(text), "a positive must react first: %r" % text)
        return texts

    def test_every_positive_followed_by_a_complaint_or_neutral_sentence_is_silent(self):
        checked, loud = 0, []
        for text in self.positives():
            for sentence in CONTINUATIONS:
                line = _with_sentence(text, sentence)
                checked += 1
                if affect.read(line) is not None:
                    loud.append((line, affect.read(line)))
        self.assertGreaterEqual(checked, 1500)
        self.assertEqual(loud, [], "\n".join(map(str, loud)))

    def test_every_positive_with_a_hedge_in_front_is_silent(self):
        checked, loud = 0, []
        for text in self.positives():
            for hedge in HEDGES:
                for line in ("%s %s" % (hedge, text), "%s, %s" % (hedge, text),
                             "%s %s" % (hedge.capitalize(), text)):
                    checked += 1
                    if affect.read(line) is not None:
                        loud.append((line, affect.read(line)))
        self.assertGreaterEqual(checked, 1500)
        self.assertEqual(loud, [], "\n".join(map(str, loud)))

    def test_every_positive_after_a_complaint_sentence_is_silent(self):
        loud = []
        for text in self.positives():
            for sentence in BEFORE:
                line = sentence + " " + text
                if affect.read(line) is not None:
                    loud.append((line, affect.read(line)))
        self.assertEqual(loud, [], "\n".join(map(str, loud)))

    def test_every_positive_with_a_turning_tail_glued_on_is_silent(self):
        loud = []
        for text in self.positives():
            for tail in TURNING:
                for joint in (", ", " ", " and ", " but ", " so "):
                    line = text.rstrip(".!?") + joint + tail
                    if affect.read(line) is not None:
                        loud.append((line, affect.read(line)))
        self.assertEqual(loud, [], "\n".join(map(str, loud[:40])))

    def test_every_positive_with_junk_in_front_is_silent(self):
        loud = []
        for text in self.positives():
            for junk in JUNK_BEFORE:
                for joint in (", ", " ", " and ", ". "):
                    line = junk + joint + text
                    if affect.read(line) is not None:
                        loud.append((line, affect.read(line)))
        self.assertEqual(loud, [], "\n".join(map(str, loud[:40])))

    def test_every_positive_padded_past_twelve_tokens_is_silent(self):
        for text in self.positives():
            self.assertIsNone(affect.read(text + " pip" * 13), text)

    def test_the_generated_checks_do_not_pass_vacuously(self):
        # the same joints with a cue-free neutral sentence behind a cue-free start give None
        # for the right reason, and a lone positive still reacts
        self.assertEqual(affect.read("I'm sad").family, "worry")
        self.assertIsNone(affect.read("I'm sad. Dinner is at eight."))
        self.assertGreaterEqual(len(POSITIVES), 150)

    def test_an_uncovered_sentence_silences_the_whole_read(self):
        self.assertEqual(affect.read("haha. thank you Pip").family, "thanks")
        self.assertIsNone(affect.read("haha. the weather is nice. thank you Pip"))
        self.assertIsNone(affect.read("thank you Pip. okay. what time is it"))
        # an all-filler sentence is empty and does not count against the read
        self.assertEqual(affect.read("thank you Pip. okay.").family, "thanks")

    def test_coverage_share_and_the_six_token_allowance(self):
        # six tokens with a cue: fine however little the cue covers
        self.assertEqual(affect.read("wish me luck on my interview").family, "support")
        # eight tokens, the cue spans six (75 percent): fine
        self.assertEqual(affect.read("I had a really bad day at the office").family, "worry")
        # twelve tokens, the cue spans six (50 percent): too little
        self.assertIsNone(affect.read("I had a really bad day at the office in the city"))
        # seven tokens, the cue spans three: too little
        self.assertIsNone(affect.read("wish me luck on my interview downtown"))

    def test_nothing_but_filler_may_come_before_the_cue(self):
        for text in ("oh well, thank you", "um okay so thank you pip", "oh hello", "yeah thanks pip"):
            self.assertIsNotNone(affect.read(text), text)
        for text in ("dinner is late thank you", "obama thank you", "my cat is sad thank you"):
            self.assertIsNone(affect.read(text), text)

    def test_what_follows_the_cue_must_open_like_a_detail_or_a_question(self):
        for text in ("I had a rough day at work", "my cat died last night",
                     "hello pip what time is it", "good morning, how did you sleep?"):
            self.assertIsNotNone(affect.read(text), text)
        for text in ("I had a rough day obama", "my cat died gross", "hello pip bye",
                     "good morning, what a joke", "hello pip what a joke",
                     "I'm so sad this is awful", "I'm sad now fix it", "I'm stressed out stop it"):
            self.assertIsNone(affect.read(text), text)

    def test_recall_given_up_is_pinned(self):
        self.assertGreaterEqual(len(RECALL_COST), 12)
        for text, _, why in RECALL_COST:
            self.assertIsNone(affect.read(text), "%s (%s)" % (text, why))

    def test_the_rule_is_documented(self):
        self.assertIn("SHORT-UTTERANCE RULE", affect.__doc__)
        self.assertEqual((affect.MAX_TOKENS, affect.SHORT_CORE, affect.COVER), (12, 6, 0.6))


class ReviewRound3(unittest.TestCase):
    """One test per class named in the second-round review (the structural rule covers
    the rest; these pin the rule changes that are about a single word list)."""

    def test_thanks_for_needs_a_whitelisted_noun_or_gerund(self):
        good = ("the help", "your help", "helping", "coming", "listening", "telling me",
                "the reminder", "the update", "the advice", "your time", "your patience",
                "everything", "yesterday", "the music", "helping me")
        bad = ("waking up the baby", "telling everyone my secret", "scaring me half to death",
               "losing my homework", "nothing", "that", "ruining it", "breaking the timer",
               "killing the process", "deleting my file", "calling customer support",
               "being so slow", "the mess", "your attitude", "the headache", "interrupting me",
               "ignoring me", "lying", "the wrong answer", "the delay", "wasting my time",
               "forgetting my name", "crashing", "the noise", "the")
        for tail in good:
            for lead in ("thanks for", "thank you for"):
                self.assertEqual(affect.read("%s %s" % (lead, tail)).family, "thanks", tail)
        for tail in bad:
            for lead in ("thanks for", "thank you for"):
                self.assertIsNone(affect.read("%s %s" % (lead, tail)), tail)
        for text in ("thanks obama", "thank you, next", "thanks a bunch", "thanks, bye"):
            self.assertIsNone(affect.read(text), text)

    def test_passed_on_is_not_a_death(self):
        for text in ("my mom passed on the leftovers", "my friend passed on the job offer"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("my mom passed away").rule, "loss")
        self.assertEqual(affect.read("my mom died").rule, "loss")

    def test_lost_job_refuses_fired_up_and_job_hunting_words(self):
        for text in ("I got fired up about the game tonight", "I was fired up all day for the match",
                     "I lost my job application form", "I lost my job search notes",
                     "I lost my job interview schedule"):
            self.assertIsNone(affect.read(text), text)
        for text in ("I lost my job", "I got fired", "I got fired today"):
            self.assertEqual(affect.read(text).rule, "lost_job", text)

    def test_kidding(self):
        self.assertIsNone(affect.read("I was kidding myself"))
        self.assertIsNone(affect.read("I'm kidding around"))
        self.assertIsNone(affect.read("I was kidding"))     # "i was kidding" is no longer a marker
        self.assertEqual(affect.read("I was joking").rule, "kidding")
        self.assertEqual(affect.read("just kidding").rule, "kidding")

    def test_failed_is_an_exam_not_a_build(self):
        for text in ("I failed the unit test again", "I failed the integration test run",
                     "I failed the test", "I failed the build", "I failed the ci pipeline test",
                     "I failed the lint check test suite", "I failed my test"):
            self.assertIsNone(affect.read(text), text)
        for text in ("I failed my driving test", "I failed my exam", "I failed the math test",
                     "I failed my interview"):
            self.assertEqual(affect.read(text).rule, "failed", text)

    def test_low_key_and_ish_are_not_down(self):
        for text in ("I'm feeling low-key excited", "I'm feeling low key hungry",
                     "I feel down-ish", "I'm feeling down ish"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("I'm feeling down").rule, "down")

    def test_anxious_is_not_anticipation_joy_or_a_denial(self):
        for text in ("I'm overwhelmed with joy", "I'm anxious to see the results",
                     "I'm scared of nothing", "I'm so nervous and excited",
                     "I'm nervous with excitement", "I'm nervous but happy",
                     "I'm nervous about the party", "I'm feeling overwhelmed",
                     "I'm stressed out of my mind with excitement"):
            self.assertIsNone(affect.read(text), text)
        for text in ("I'm nervous", "I'm so anxious", "I'm scared", "I'm terrified"):
            self.assertEqual(affect.read(text).family, "support", text)

    def test_crying_and_rough_day_context(self):
        self.assertIsNone(affect.read("I'm crying, I'm cutting onions"))
        self.assertIsNone(affect.read("I'm crying from the onion"))
        self.assertIsNone(affect.read("today was a bad day for the stock market"))
        self.assertIsNone(affect.read("it's been a rough week for the whole office"))
        self.assertEqual(affect.read("I'm crying").family, "worry")
        self.assertEqual(affect.read("it's been a rough week").family, "worry")

    def test_laugh_must_end_the_sentence(self):
        for text in ("haha, you wish", "lmao stop", "lol whatever", "haha that's hilarious",
                     "haha, I know"):
            self.assertIsNone(affect.read(text), text)
        for text in ("haha", "lol pip", "hahaha!", "ha ha ha"):
            self.assertEqual(affect.read(text).family, "humour", text)

    def test_surprise_follow_ons(self):
        for text in ("oh my god, please stop", "omg, you talk too much", "no way, put that down!",
                     "I can't believe it, you talk too much", "oh my god you are slow",
                     "wow. you are so slow."):
            self.assertIsNone(affect.read(text), text)
        for text in ("oh my god", "omg!", "I can't believe it", "oh my god pip", "wow"):
            self.assertEqual(affect.read(text).family, "surprise", text)

    def test_the_new_safe_rows_end_the_sentence(self):
        for text, rule in (("nice to meet you", "meet"), ("Nice to meet you, Pip", "meet"),
                           ("Congratulations", "congrats"), ("congrats Pip", "congrats"),
                           ("I'm so proud of you", "proud"), ("proud of you Pip", "proud"),
                           ("I really appreciate your help", "appreciate_help"),
                           ("appreciate your help", "appreciate_help")):
            self.assertEqual(affect.read(text).rule, rule, text)
        for text in ("nice to meet you, you are late", "congratulations, you broke it",
                     "congratulations pip you did it", "I'm proud of you for nothing",
                     "I appreciate your help, not", "appreciate your help but you were slow",
                     "if I appreciate your help", "not proud of you",
                     "nice to meet you said nobody"):
            self.assertIsNone(affect.read(text), text)

    def test_praise_must_be_the_whole_sentence(self):
        for text in ("nice job, now the lights are all off", "well done pip, the soup is cold now",
                     "good job. you deleted my alarm", "good job pip, good job, you broke the toaster"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("good job pip").rule, "praise")

    def test_a_worry_then_thanks_is_a_dig(self):
        for text in ("I'm anxious, thank you so much", "I'm stressed, thanks so much for that",
                     "I'm sad, thanks"):
            self.assertIsNone(affect.read(text), text)
        self.assertEqual(affect.read("thanks, I had a rough day").family, "worry")


class Bounded(unittest.TestCase):
    def test_deterministic(self):
        for text in [t for t, _ in POSITIVES[:60]] + NEGATIVES[:60]:
            self.assertEqual(affect.read(text), affect.read(text))
        words = "thank you good morning I'm sad haha " * 2000
        self.assertEqual(affect.read(words), affect.read(words))

    def test_ten_thousand_words_is_fast_and_bounded(self):
        filler = " ".join("word%d" % (i % 50) for i in range(10000))
        start = time.perf_counter()
        for _ in range(20):
            self.assertIsNone(affect.read(filler))
        per_call = (time.perf_counter() - start) / 20
        self.assertLess(per_call, 0.05, "read() on 10,000 words took %.1f ms" % (per_call * 1000))

    def test_an_utterance_of_more_than_twelve_tokens_gives_none(self):
        # 12 tokens of cue plus filler (pip is filler) still read; 13 do not.
        twelve = "thank you" + " pip" * 10
        self.assertEqual(len(twelve.split()), 12)
        self.assertEqual(affect.read(twelve).family, "thanks")
        self.assertIsNone(affect.read(twelve + " pip"))
        self.assertEqual(affect.MAX_TOKENS, 12)
        # "i am" folds to "i'm", so the cap counts folded tokens
        self.assertEqual(affect.read("i am sad" + " pip" * 10).family, "worry")
        # a long text after or before a real cue never reads, however the cue stands
        self.assertIsNone(affect.read("good morning, " + "banana " * 10000))
        self.assertIsNone(affect.read("banana " * 10000 + " good morning"))
        self.assertIsNone(affect.read(" ".join(["banana"] * 60) + " I'm sad"))

    def test_a_text_longer_than_the_character_cap_gives_none(self):
        self.assertEqual(affect.read("I'm sad" + "!" * 100).family, "worry")
        self.assertIsNone(affect.read("I'm sad" + "!" * 5000))
        self.assertIsNone(affect.read("thank you" + " " * 2000))

    def test_a_single_enormous_token_and_odd_input_do_not_hang_or_raise(self):
        weird = [
            "x" * 1_000_000,
            "." * 100_000,
            ",".join(["pip"] * 5000),
            "\u0000" * 500,
            "?!" * 5000,
            "ha" * 50000,
            "i'm " * 5000,
            "😀 " * 3000,
            "'" * 10000,
            "1.2.3.4.5 " * 3000,
        ]
        start = time.perf_counter()
        for text in weird:
            affect.read(text)
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_it_is_pure_no_state_leaks_between_reads(self):
        first = affect.read("my dog died")
        affect.read("good morning")
        affect.read("thanks for nothing")
        self.assertEqual(affect.read("my dog died"), first)


class AgainstTheTable(unittest.TestCase):
    """Cross-checks with the Rust half (crates/brainstem/data/emotion-director.json)."""

    def table(self):
        if not TABLE.exists():
            self.skipTest("%s does not exist yet (the Rust builder creates it); "
                          "family ids not cross-checked" % TABLE)
        return json.loads(TABLE.read_text())

    def test_every_family_id_exists_in_the_table(self):
        table = self.table()
        ids = {f["id"] for f in table["families"]}
        missing = [f for f in affect.FAMILIES if f not in ids]
        self.assertEqual(missing, [], "family ids the mind knows but the table lacks")

    def test_the_table_has_no_family_the_mind_does_not_know(self):
        ids = {f["id"] for f in self.table()["families"]}
        self.assertEqual(ids - set(affect.FAMILIES), set())

    def test_the_table_ladders_match_the_contract(self):
        table = self.table()
        got = {f["id"]: tuple(r["pose"] for r in f["rungs"]) for f in table["families"]}
        for family, ladder in LADDERS.items():
            self.assertEqual(got.get(family), ladder, family)

    def rung_pose(self, cue):
        """The pose the brainstem would pick: the highest rung whose min_strength is met."""
        pose = None
        for rung in self.table()["families"][self.family_index(cue.family)]["rungs"]:
            if cue.strength >= rung["min_strength"]:
                pose = rung["pose"]
        return pose

    def family_index(self, family):
        return [f["id"] for f in self.table()["families"]].index(family)

    def test_each_positive_lands_on_a_rung_its_rule_allows(self):
        # Not vacuous: rung 0 starts at 0.0, so "at least rung 0" proves nothing.
        # Every positive must land on a pose from its rule's own allowed list, so
        # a strength change that lifts a mild line to a loud pose fails here.
        wrong = []
        for text, _ in POSITIVES:
            cue = affect.read(text)
            pose = self.rung_pose(cue)
            allowed = RULE_POSES.get(cue.rule)
            if allowed is None or pose not in allowed:
                wrong.append((text, cue.rule, cue.strength, pose, allowed))
        self.assertEqual(wrong, [], "\n".join(map(str, wrong)))

    def test_pinned_lines_land_on_the_named_pose(self):
        wrong = []
        for text, pose in RUNG_PINS:
            cue = affect.read(text)
            got = self.rung_pose(cue) if cue else None
            if got != pose:
                wrong.append((text, pose, got, cue))
        self.assertEqual(wrong, [], "\n".join(map(str, wrong)))

    def test_every_rule_has_an_allowed_pose_list(self):
        self.assertEqual({r[0] for r in affect_lexicon_rules()} - set(RULE_POSES), set())

    def test_the_scan_limits_are_separate_and_documented(self):
        # The Rust table bounds its lexicon over Pip's own reply clauses; this
        # module bounds the person's accepted turn. They are different scans.
        scan = self.table()["scan"]
        self.assertGreaterEqual(affect.MAX_WORDS, scan["max_words"])
        self.assertGreaterEqual(affect.MAX_CHARS, scan["max_bytes"])
        self.assertIn("SEPARATE scans", affect.__doc__)
        self.assertIn(str(scan["max_words"]), affect.__doc__)
        self.assertIn(str(scan["max_bytes"]), affect.__doc__)

    def test_the_hold_range_matches_the_wire_limits(self):
        wire = self.table()["wire"]
        self.assertEqual((affect.HOLD_MIN, affect.HOLD_MAX), (wire["hold_min"], wire["hold_max"]))
        self.assertGreaterEqual(affect.DEFAULT_HOLD, wire["hold_min"])
        self.assertLessEqual(affect.DEFAULT_HOLD, wire["hold_max"])


class LaddersAgainstTheSheet(unittest.TestCase):
    """The contract's poses exist in the face catalogue and are group a or a*."""

    def test_every_ladder_pose_is_live_in_the_catalogue_and_group_a(self):
        if not (CONCEPTS.exists() and GROUPS.exists()):
            self.skipTest("face catalogue or groups sheet not present")
        names = {p["name"] for p in json.loads(CONCEPTS.read_text())["poses"]}
        groups = {row["name"]: row["group"] for row in json.loads(GROUPS.read_text())}
        for family, ladder in LADDERS.items():
            for pose in ladder:
                self.assertIn(pose, names, "%s/%s missing from the face catalogue" % (family, pose))
                self.assertIn(groups.get(pose), ("a", "a*"), "%s/%s is group %s" % (family, pose, groups.get(pose)))


def bench(runs=20000):
    """Microseconds per read(), by input shape. Labelled host."""
    sentence = ("well the meeting went on much longer than I expected today and then my sister "
                "called about the trip so thank you very much for the reminder Pip")
    assert len(sentence.split()) == 28, len(sentence.split())
    twelve = "hello pip can you set a timer for me in ten minutes"
    assert len(twelve.split()) == 12, len(twelve.split())
    out = {}
    for label, text in (("28-word sentence with a cue at the end (over the cap)", sentence),
                        ("12-token line, cue plus a request", twelve),
                        ("12-token line with a worry cue", "I had a really bad day at work today and I am tired"),
                        ("short cue (I'm sad)", "I'm sad"),
                        ("two short sentences with cues", "haha. thank you Pip"),
                        ("60-word scan cap, no cue (about 600 chars)", " ".join(["banana"] * 100))):
        affect.read(text)
        start = time.perf_counter()
        for _ in range(runs):
            affect.read(text)
        out[label] = (time.perf_counter() - start) / runs * 1e6
    return out


class Benchmark(unittest.TestCase):
    def test_micro_benchmark_prints_host_cost(self):
        results = bench(runs=2000)
        for label, us in results.items():
            print("\n  host: %.1f us per read(), %s" % (us, label))
            self.assertTrue(math.isfinite(us))
        # a generous ceiling: the cost must stay far below one frame on the host
        self.assertLess(max(results.values()), 2000.0)


if __name__ == "__main__":
    if "--bench" in sys.argv:
        for label, us in bench().items():
            print("host: %7.1f us per read()  (%s)" % (us, label))
    else:
        unittest.main()
