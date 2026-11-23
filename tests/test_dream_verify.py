"""The dream model's items are checked by code, and rejected, never repaired."""

import unittest

from mind.dream_verify import Line, check, check_all

LINES = {
    101: Line(101, "owner", "My orchid is called Silverleaf, and it flowers every March."),
    102: Line(102, "being", "Silverleaf is a lovely name for an orchid."),
    103: Line(103, "owner", "My sister Anna's birthday is on the 14th of March."),
    104: Line(104, "owner", "I don't eat meat any more, not since 2019."),
    105: Line(105, "owner", "Sam said the plumber is coming on Thursday at 9:30."),
    106: Line(106, "owner", "If I won the lottery I'd buy a boat called Seagull."),
    107: Line(107, "owner", "I’m going to Japan in two weeks with my brother Tom."),
    108: Line(108, "other", "I'm Margaret from next door, I water the tomatoes."),
    109: Line(109, "owner", "We keep the spare key in the blue pot by the door."),
    110: Line(110, "owner", "I really love strong black coffee in the morning."),
    111: Line(111, "owner", "My sister is visiting tomorrow for lunch."),
    112: Line(112, "owner", "Annabel is coming over on Friday."),
    113: Line(113, "owner", "Did you know a group of crows is called a murder?"),
    114: Line(114, "owner", "So I probably can't take Friday off after all, I'm not sure yet."),
    115: Line(115, "owner", "Pip, you'd make a terrible chef, you can't even taste."),
    116: Line(116, "owner", "My mum used to make it for me when I was little."),
    117: Line(117, "owner", "I might paint the bedroom green, or maybe grey, who knows."),
    118: Line(118, "owner", "My nephew Leo turns six on the 9th of December. Is that a big party?"),
    119: Line(119, "owner", "My colleague Priya says the office is closing early on Christmas Eve."),
}


def item(**changes):
    value = {
        "kind": "fact", "claim": "The owner's orchid is called Silverleaf.",
        "quote": "My orchid is called Silverleaf", "episode_id": "E101", "speaker": "owner",
        "modality": "stated", "time_ref": "",
    }
    value.update(changes)
    return value


VALID = [
    item(),
    item(claim="The orchid flowers every March.", quote="it flowers every March"),
    item(episode_id="E103", claim="Anna is the owner's sister.", quote="My sister Anna's birthday"),
    item(episode_id="E103", kind="event", claim="Anna's birthday is on 14 March.",
         quote="My sister Anna's birthday is on the 14th of March.", time_ref="14th of March"),
    item(episode_id="E104", kind="preference", claim="The owner doesn't eat meat.", quote="I don't eat meat"),
    item(episode_id="E105", kind="event", modality="reported",
         claim="Sam said the plumber comes on Thursday at 9:30.",
         quote="Sam said the plumber is coming on Thursday at 9:30", time_ref="Thursday at 9:30"),
    item(episode_id="E107", kind="plan", claim="The owner is going to Japan with Tom in two weeks.",
         quote="I'm going to Japan in two weeks with my brother Tom", time_ref="in two weeks"),
    item(episode_id="E108", speaker="other", kind="relation", claim="Margaret lives next door.",
         quote="I'm Margaret from next door"),
    item(episode_id="E109", claim="The spare key is in the blue pot.",
         quote="the spare key in the blue pot by the door"),
    item(episode_id="E110", kind="preference", claim="The owner loves strong black coffee.",
         quote="  I REALLY love strong   black coffee  "),
    item(episode_id="E101", claim="Silverleaf flowers in March.", quote="“Silverleaf, and it flowers every March”"),
    # "early" is quoted, though its stem is not the word itself.
    item(episode_id="E119", kind="event", modality="reported",
         claim="Priya says the office is closing early on Christmas Eve.",
         quote="My colleague Priya says the office is closing early on Christmas Eve"),
    # A question after the quoted sentence does not taint it.
    item(episode_id="E118", kind="event", claim="The owner's nephew Leo turns six on the 9th of December.",
         quote="My nephew Leo turns six on the 9th of December", time_ref="9th of December"),
]

ADVERSARIAL = [
    ("not_an_object", ["E101", "my orchid"]),
    ("wrong_fields", {k: v for k, v in item().items() if k != "time_ref"}),
    ("wrong_fields", {**item(), "confidence": 1.0}),
    ("wrong_types", item(episode_id=101)),
    ("wrong_types", item(claim=None)),
    ("unknown_kind", item(kind="secret")),
    ("unknown_speaker", item(speaker="Richard")),
    ("unknown_modality", item(modality="certain")),
    ("not_asserted", item(episode_id="E106", modality="hypothetical", claim="The owner would buy a boat.",
                          quote="If I won the lottery I'd buy a boat")),
    ("not_asserted", item(modality="joking")),
    ("not_asserted", item(modality="unsure")),
    ("claim_length", item(claim="")),
    ("claim_length", item(claim="The owner's orchid is called Silverleaf " + "and it is lovely " * 10)),
    ("quote_length", item(quote="orchid")),
    ("quote_length", item(quote="My orchid is called Silverleaf" * 8)),
    ("control_characters", item(claim="The owner's orchid is called\x1b[2J Silverleaf.")),
    ("bad_episode_id", item(episode_id="101")),
    ("bad_episode_id", item(episode_id="E101 OR 1=1")),
    ("bad_episode_id", item(episode_id="E0101")),
    ("episode_not_in_chunk", item(episode_id="E999")),
    ("being_line", item(episode_id="E102", speaker="owner", claim="Silverleaf is a lovely name.",
                        quote="Silverleaf is a lovely name for an orchid")),
    ("being_line", item(episode_id="E102", speaker="being", claim="Silverleaf is a lovely name.",
                        quote="Silverleaf is a lovely name for an orchid")),
    ("being_line", item(speaker="being")),
    ("speaker_mismatch", item(episode_id="E108", speaker="owner", claim="Margaret lives next door.",
                              quote="I'm Margaret from next door")),
    ("quote_not_in_episode", item(quote="My orchid is called Moonleaf")),
    ("quote_not_in_episode", item(quote="My orchid is named Silverleaf")),
    ("quote_not_in_episode", item(episode_id="E103", quote="My orchid is called Silverleaf")),
    ("quote_not_in_episode", item(quote="orchid is called Silverleaf, and it flowers every May")),
    ("quote_not_in_episode", item(episode_id="E109", quote="key in the blue pot by the doorway")),
    ("number_not_in_quote", item(episode_id="E103", claim="Anna's birthday is on 15 March.",
                                 quote="My sister Anna's birthday is on the 14th of March")),
    ("number_not_in_quote", item(episode_id="E107", claim="The owner goes to Japan in three weeks.",
                                 quote="I'm going to Japan in two weeks")),
    ("number_not_in_quote", item(episode_id="E104", claim="The owner stopped eating meat in 2018.",
                                 quote="I don't eat meat any more")),
    ("name_not_in_quote", item(claim="The owner's orchid Silverleaf was a gift from Anna.")),
    ("name_not_in_quote", item(episode_id="E103", claim="Maria is the owner's sister.",
                               quote="My sister Anna's birthday")),
    ("name_not_in_quote", item(claim="The orchid flowers every April.", quote="it flowers every March")),
    ("name_not_in_quote", item(episode_id="E105", modality="reported", claim="The plumber comes on Friday.",
                               quote="the plumber is coming on Thursday")),
    ("negation_mismatch", item(episode_id="E104", kind="preference", claim="The owner eats meat.",
                               quote="I don't eat meat any more")),
    ("negation_mismatch", item(episode_id="E110", kind="preference", claim="The owner does not love coffee.",
                               quote="I really love strong black coffee")),
    ("claim_unsupported", item(episode_id="E109", claim="The owner keeps money and jewellery hidden.",
                               quote="We keep the spare key in the blue pot by the door")),
    ("claim_unsupported", item(episode_id="E110", claim="The owner drinks herbal tea at bedtime.",
                               quote="I really love strong black coffee in the morning")),
    ("quote_not_in_episode", item(quote="")),
    # A name hidden inside a longer word of the quote is still invented.
    ("name_not_in_quote", item(episode_id="E111", kind="event", claim="Tom is visiting tomorrow.",
                               quote="is visiting tomorrow for lunch")),
    ("name_not_in_quote", item(episode_id="E112", kind="event", claim="Ann is coming over on Friday.",
                               quote="Annabel is coming over on Friday")),
    # Who someone is, and which time, are not the model's to change.
    ("relation_not_in_quote", item(episode_id="E103", claim="Anna is the owner's brother.",
                                   quote="My sister Anna's birthday")),
    ("relation_not_in_quote", item(episode_id="E107", kind="plan", claim="The owner goes to Japan with their son Tom.",
                                   quote="I'm going to Japan in two weeks with my brother Tom")),
    ("time_not_in_quote", item(episode_id="E107", kind="plan", claim="The owner is going to Japan next week.",
                               quote="I'm going to Japan in two weeks")),
    ("time_ref_length", item(time_ref="every single morning of every week of every year forever")),
    # Found in the real-model run of 24 November 2026: labelled "stated" by
    # the model, but a question, a hedge, a remark about Pip, and a claim
    # carrying words from another line.
    ("question", item(episode_id="E113", claim="A group of crows is called a murder.",
                      quote="a group of crows is called a murder")),
    ("hedged", item(episode_id="E114", kind="plan", claim="The owner can't take Friday off.",
                    quote="I probably can't take Friday off after all")),
    ("hedged", item(episode_id="E117", kind="plan", claim="The owner will paint the bedroom green.",
                    quote="paint the bedroom green")),
    ("hedged", item(episode_id="E106", claim="The owner would buy a boat called Seagull.",
                    quote="I'd buy a boat called Seagull")),
    ("about_being", item(episode_id="E115", kind="preference", claim="The owner thinks Pip would be a terrible chef.",
                         quote="Pip, you'd make a terrible chef")),
    ("claim_unsupported", item(episode_id="E116", claim="The owner's mum used to make ginger tea with honey.",
                               quote="My mum used to make it for me when I was little")),
    ("question", item(episode_id="E118", claim="Leo is having a big party.", quote="Is that a big party")),
]


class Verification(unittest.TestCase):
    def test_at_least_ten_valid_items_are_all_accepted(self):
        self.assertGreaterEqual(len(VALID), 10)
        for proposed in VALID:
            with self.subTest(claim=proposed["claim"]):
                verdict = check(proposed, LINES)
                self.assertTrue(verdict.ok, verdict.reason)

    def test_at_least_thirty_adversarial_items_are_all_rejected_for_the_right_reason(self):
        self.assertGreaterEqual(len(ADVERSARIAL), 30)
        for reason, proposed in ADVERSARIAL:
            with self.subTest(reason=reason, item=proposed):
                verdict = check(proposed, LINES)
                self.assertFalse(verdict.ok)
                self.assertIsNone(verdict.item)
                expected = "quote_length" if reason == "quote_not_in_episode" and proposed["quote"] == "" else reason
                self.assertEqual(verdict.reason, expected)

    def test_the_kept_quote_is_the_stored_wording_and_the_claim_is_untouched(self):
        verdict = check(VALID[9], LINES)
        self.assertEqual(verdict.item.quote, "I really love strong black coffee")
        self.assertEqual(verdict.item.claim, VALID[9]["claim"])
        curly = check(item(episode_id="E107", kind="plan", claim="The owner is going to Japan.",
                           quote="I'm going to Japan"), LINES)
        self.assertEqual(curly.item.quote, "I’m going to Japan")

    def test_a_quote_must_be_whole_words(self):
        lines = {5: Line(5, "owner", "Remember to concatenate the files tonight please.")}
        verdict = check(item(episode_id="E5", claim="The owner wants to catenate files.",
                             quote="catenate the files tonight"), lines)
        self.assertEqual(verdict.reason, "quote_not_in_episode")

    def test_names_relations_and_times_that_are_quoted_pass(self):
        for proposed in (
            item(episode_id="E111", kind="event", claim="The owner's sister is visiting tomorrow.",
                 quote="My sister is visiting tomorrow"),
            item(episode_id="E112", kind="event", claim="Annabel is coming over on Friday.",
                 quote="Annabel is coming over on Friday"),
        ):
            with self.subTest(claim=proposed["claim"]):
                self.assertTrue(check(proposed, LINES).ok)

    def test_the_same_claim_in_other_words_has_one_signature(self):
        from mind.dream_verify import claim_signature

        self.assertEqual(claim_signature("The owner's dog is called Biscuit."),
                         claim_signature("Biscuit is the name of the owner's dog"))
        self.assertEqual(claim_signature("The owner doesn't like coffee."),
                         claim_signature("The owner does not like coffee"))
        for one, other in (("The owner likes coffee.", "The owner doesn't like coffee."),
                           ("The owner wants a dog.", "The owner has a dog."),
                           ("The owner's sister lives in Perth.", "The owner lives in Perth.")):
            self.assertNotEqual(claim_signature(one), claim_signature(other))

    def test_a_claim_about_the_being_goes_by_its_configured_name(self):
        lines = dict(LINES)
        lines[120] = Line(120, "owner", "Juniper, you'd make a terrible chef, you can't even taste.")
        lines[121] = Line(121, "owner", "My friend Pip is a chef in Hobart.")
        about = item(episode_id="E120", kind="preference", claim="The owner thinks Juniper would be a terrible chef.",
                     quote="Juniper, you'd make a terrible chef")
        friend = item(episode_id="E121", claim="The owner's friend Pip is a chef in Hobart.",
                      quote="My friend Pip is a chef in Hobart")
        self.assertEqual(check(about, lines, "Juniper").reason, "about_being")
        self.assertTrue(check(friend, lines, "Juniper").ok)
        self.assertEqual(check(friend, lines).reason, "about_being")
        accepted, rejected = check_all([about, friend], lines, being="Juniper")
        self.assertEqual((len(accepted), rejected), (1, ["about_being"]))

    def test_a_batch_reads_at_most_eight_items(self):
        accepted, rejected = check_all([item()] * 10, LINES)
        self.assertEqual(len(accepted), 8)
        self.assertEqual(rejected, ["too_many_items", "too_many_items"])
        self.assertEqual(check_all({"items": []}, LINES), ([], ["not_a_list"]))


if __name__ == "__main__":
    unittest.main()
