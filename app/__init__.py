"""The robot application: process lifecycle, the loops, and the camera supervisor.

face_track.py is the entry point and the composition root — it parses the CLI, builds the
collaborators (servo, camera, voice assistant, conversation session, dashboard) and hands them to
the pieces here. Nothing in this package constructs those collaborators itself, so each module can
be imported and exercised without the hardware the others need.

Layering, outermost first:

    face_track.py       CLI + wiring
      app/              lifecycle, tracking loop, control loop, camera supervisor
        web/            the dashboard (state + Flask routes)
        ai/  vision/  servo/     the subsystems
          config/  settings.py   the knobs

Dependencies point downward only. app/ imports ai/, vision/, servo/ and web/; none of those
imports app/, which is what keeps them testable on a machine with no camera and no mic.
"""
