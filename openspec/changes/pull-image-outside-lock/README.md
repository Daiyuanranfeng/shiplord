# pull-image-outside-lock

Move the image pull before the host-wide lock so a slow registry cannot block other deploys or read-only admin commands
