# College Football Confidence Picks (CFCP) - Official Rules

Welcome to College Football Confidence Picks! This bot automates a competitive college football confidence pool. Before you make your first pick, please review the rules below.

## 1. The Matchups (Ranked Teams Only)
You won't be picking every single game on the Saturday slate. Each week's pick sheet is strictly curated to feature **only matchups involving at least one ranked team**. 
* **Early Season:** Matchups are determined using the **AP Top 25** poll.
* **Late Season:** Once the selection committee releases them, the system officially switches to use the **College Football Playoff (CFP) Top 25** rankings.

## 2. The Core Objective
In a confidence pool, you don't just pick the winner of a game; you also assign a "confidence point" value to each of your picks. The more confident you are that a team will win, the higher the point value you should assign to that matchup.

## 3. Assigning Confidence Points
* The number of confidence points available to you depends on the total number of games being played that week. 
* *Example:* If there are 15 games in a week, you will assign values `1` through `15` to your picks.
* You can only use each point value exactly **once**. 

## 4. Scoring System
* **Winning a Pick:** If the team you selected wins the game outright, you are awarded the number of confidence points you assigned to that matchup.
* **Losing a Pick:** If the selected team loses, you receive `0` points for that matchup.
* **Ties:** In the rare event that a game officially ends in a tie, no winner is credited. All players will receive `0` points for that game, regardless of who they picked.

## 5. Deadlines & Editing Picks
Picks lock at the exact kickoff time of each individual game. You can freely submit, change, or mathematically shift your confidence points for any game right up until the moment it kicks off. 

Once a game kicks off, it is permanently locked and your pick (and its point value) cannot be changed.

## 6. Missed Picks & The Penalty System (Forfeits)
If a game kicks off and you forgot to submit a pick for it, you will forfeit that game. The system enforces a strict penalty for forfeits:

**The bot will automatically take your lowest available (unused) confidence point slot at the exact moment the game locks and burn it.**

* **Example A (Standalone Game):** If you wait until Saturday to make your picks, but miss a lone Friday night game, you forfeit it. Since you haven't used any points yet, the system penalizes you by burning your `1-point` slot.
* **Example B (Multiple Games):** If you forget to submit picks for a Saturday 12:00 PM slate that features 5 games, the system will penalize you by taking your 5 lowest available point slots at that exact time (e.g., burning your `1`, `2`, `3`, `4`, and `5` point slots).
* **Example C (End of Day):** If you make every pick perfectly except for the final late-night game, and your only remaining unused point value is your `10-point` slot, you will be penalized with the loss of your `10-point` slot for missing that game.

## 7. Standings & Tiebreakers
* **Leaderboards:** Players are ranked on a weekly and season-long basis solely by the total number of points they have accumulated. 
* **Tiebreakers:** There are no tiebreakers. If two or more players finish a week with the exact same number of points, they are all recognized as officially tying for that position.