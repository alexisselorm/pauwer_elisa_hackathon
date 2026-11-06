# Use case: turning hidden expert knowledge into operational intelligence

*Example provided by Elisa for the Since AI Hackathon 2026 challenge "Capturing and Linking Human-Generated Contextual Metadata for Autonomous Network Operations".*

A field engineer encounters an unusual network behaviour that cannot be resolved using existing documentation or standard procedures.

The field engineer contacts **Erkki Expert**, who recognises the situation from previous experience. Erkki knows how to interpret the combination of symptoms and which action should be taken, but **this knowledge has never been formally documented**.

The knowledge exists only in:

- Expert experience and memory
- Teams conversations
- Emails
- Informal expert-to-expert discussions
- Previous troubleshooting cases

## Example

- A network interface shows **UP**, optical levels are normal and alarms have cleared, but traffic has not recovered after an SFP replacement.
- The field engineer assumes that the SFP may still be faulty.
- Erkki, however, knows from experience: **Interface UP + normal optical levels + zero traffic counters after SFP replacement → likely a forwarding-state issue rather than a faulty SFP.**
- He advises the field engineer to disable and re-enable the interface before replacing hardware or rebooting the device.
- The problem is resolved.

The same case is in `teams_chat.json` (Teams discussion) and `email.txt` (email).

## Second example: access information

There was a device problem in Northern Finland. AI analysed that the problem was with the device in question, and a field engineer was sent to the site. When the field engineer approached the site, a locked barrier at the beginning of the private road prevented him from reaching it. The autonomous cycle was interrupted and a human analysis had to start, which found the key to the barrier and allowed the work to continue. Information about this barrier was not in any centralised system, but in an email.
