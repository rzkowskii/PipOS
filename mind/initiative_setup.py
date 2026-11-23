"""Setup controls for automatic announcements and quiet hours."""

from dataclasses import replace

from .initiative import QuietHours, load_policy, save_policy


def describe_policy(policy):
    window = policy.quiet_hours
    quiet = f"{window.start} to {window.end} (local time)" if window else "Off"
    return (f"Automatic reminders and timers   {'On' if policy.reminders else 'Off'}\n"
            f"Daily greetings                  {'On' if policy.greetings else 'Off'}\n"
            f"Morning brief                    {'On' if policy.brief else 'Off'}\n"
            f"Quiet hours                      {quiet}")


def configure_initiative(mind_dir, *, dry_run=False, temporary=False):
    policy = load_policy(mind_dir)
    print("\nQuiet hours and announcements")
    print("Quiet hours pause automatic speech.\nDue reminders and timers stay pending.")
    print("Daily greetings use the separately enabled camera.")
    print("The morning brief answers your first words of the day.")
    while True:
        print(f"\n{describe_policy(policy)}")
        print("\n  1  Toggle automatic reminders\n  2  Toggle daily greetings\n  3  Set quiet hours"
              "\n  4  Clear quiet hours\n  5  Toggle morning brief\n  K  Save and go back\n  Q  Discard and go back")
        choice = input("Choice: ").strip().lower()
        if choice == "1":
            policy = replace(policy, reminders=not policy.reminders)
        elif choice == "2":
            policy = replace(policy, greetings=not policy.greetings)
        elif choice == "3":
            start = input("Start (HH:MM, Enter to go back): ").strip()
            if not start:
                continue
            end = input("End (HH:MM, Enter to go back): ").strip()
            if not end:
                continue
            try:
                policy = replace(policy, quiet_hours=QuietHours(start, end))
            except ValueError as error:
                print(error)
        elif choice == "4":
            policy = replace(policy, quiet_hours=None)
        elif choice == "5":
            policy = replace(policy, brief=not policy.brief)
        elif choice == "k":
            if dry_run:
                print("Announcement preview finished.")
            else:
                mind_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                save_policy(mind_dir, policy)
                print("Announcement settings saved for this Try session." if temporary else "Announcement settings saved.")
            return 0
        elif choice == "q":
            return 0
        else:
            print("Choose a number, K, or Q.")
