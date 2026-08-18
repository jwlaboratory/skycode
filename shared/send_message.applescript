on run argv
	set theTarget to item 1 of argv
	set theMessage to item 2 of argv
	tell application "Messages"
		set theService to 1st account whose service type = iMessage
		set theBuddy to participant theTarget of theService
		send theMessage to theBuddy
	end tell
end run
