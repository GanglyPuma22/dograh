extends RefCounted


func run(test: RefCounted) -> bool:
	_run_capture_gate(test)
	_run_overlay_state(test)
	_run_binding_remap(test)
	_run_wake_voice_lifecycle(test)
	return true


func _run_capture_gate(test: RefCounted) -> void:
	var capture := CompanionAudioCapture.new()
	capture.activation_required = true
	var starts := [0]
	capture.capture_started.connect(func() -> void: starts[0] += 1)
	capture.handle_push_to_talk(true)
	test.assert_equal(starts[0], 0, "dormant talk does not open microphone or turn")
	capture.set_encounter_active(true)
	test.assert_equal(starts[0], 0, "wake does not open microphone or turn")
	capture.handle_push_to_talk(true)
	test.assert_equal(starts[0], 1, "awake explicit talk opens capture")
	capture.set_encounter_active(false)
	test.assert_equal(capture.is_capturing(), false, "reset aborts capture")
	capture.free()


func _run_overlay_state(test: RefCounted) -> void:
	var overlay := CompanionOverlay.new()
	overlay.set_character_name("Aster")
	overlay.handle_service_status(&"speaking")
	test.assert_equal(
		overlay.view_model().status,
		&"thinking",
		"queued server speech is not reported as local playback",
	)
	overlay.handle_audio_pending("live-audio", 24000, 1, &"pcm_s16le")
	test.assert_equal(
		overlay.view_model().status,
		&"thinking",
		"an audio header after server speech remains queued",
	)
	overlay.handle_service_status(&"speaking")
	test.assert_equal(
		overlay.view_model().status,
		&"thinking",
		"an audio header does not make queued server speech audible",
	)
	overlay.handle_playback_started("live-audio")
	test.assert_equal(
		overlay.view_model().status,
		&"speaking",
		"buffered playback establishes local speaking status",
	)
	test.assert_equal(
		overlay.view_model().status_text,
		"ASTER SPEAKING",
		"real playback owns speaking UI",
	)
	overlay.handle_playback_interrupted("live-audio")
	test.assert_equal(
		overlay.view_model().audio_turn_id,
		"",
		"interruption clears the audible turn",
	)
	overlay.set_encounter_view(
		&"awake",
		LiltEncounterState.GREETING,
		"Hold [Backtick] to talk to Aster",
	)
	test.assert_true(
		overlay.view_model().encounter_greeting.begins_with("Oh—hello"),
		"the greeting remains available without voice transport",
	)
	overlay.handle_companion_error(
		&"authentication_required",
		"secret must never display",
		true,
	)
	test.assert_equal(
		overlay.view_model().status_text,
		"ASTER SETUP REQUIRED",
		"configuration failure uses the fixed offline status",
	)
	overlay.free()


func _run_binding_remap(test: RefCounted) -> void:
	var original := InputMap.action_get_events(CompanionAudioCapture.INPUT_ACTION)
	InputMap.action_erase_events(CompanionAudioCapture.INPUT_ACTION)
	var backtick := InputEventKey.new()
	backtick.physical_keycode = KEY_QUOTELEFT
	InputMap.action_add_event(CompanionAudioCapture.INPUT_ACTION, backtick)
	test.assert_equal(
		LiltEncounter.talk_binding_label(),
		"Backtick",
		"the default backtick binding has a readable label",
	)
	InputMap.action_erase_events(CompanionAudioCapture.INPUT_ACTION)
	var remapped := InputEventKey.new()
	remapped.physical_keycode = KEY_V
	remapped.ctrl_pressed = true
	InputMap.action_add_event(CompanionAudioCapture.INPUT_ACTION, remapped)
	test.assert_equal(
		LiltEncounter.talk_binding_label(),
		"Ctrl+V",
		"the prompt resolves the current modifier and key",
	)
	InputMap.action_erase_events(CompanionAudioCapture.INPUT_ACTION)
	test.assert_equal(
		LiltEncounter.talk_binding_label(),
		"unbound",
		"a missing talk binding stays honest",
	)
	for event: InputEvent in original:
		InputMap.action_add_event(CompanionAudioCapture.INPUT_ACTION, event)


class WakeRecordingClient extends CompanionClient:
	var frames: Array[Dictionary] = []

	func _send_control(payload: Dictionary) -> Error:
		frames.append(payload.duplicate(true))
		return OK


func _wake_fixture(capabilities: Array = ["game_events_v1", "aster_wake_v1"]) -> LiltEncounter:
	var encounter := LiltEncounter.new()
	var client := WakeRecordingClient.new()
	client.auto_connect = false
	client.begin_handshake()
	client.accept_server_text(JSON.stringify({
		"type": "hello_ack", "protocol_version": 1, "session_id": "wake-unit", "companion": "Aster",
		"audio": {"format": "pcm_s16le", "sample_rate": 24000, "channels": 1},
		"capabilities": capabilities,
	}))
	encounter._client = client
	encounter._connection_requested = true
	encounter.wake_voice_enabled = true
	return encounter


func _free_wake_fixture(encounter: LiltEncounter) -> void:
	encounter._cancel_wake_voice()
	encounter._client.free()
	encounter._client = null
	encounter.free()


func _run_wake_voice_lifecycle(test: RefCounted) -> void:
	var encounter := _wake_fixture()
	test.assert_true(encounter.activate_lilt(), "fresh activation queues one greeting")
	encounter._advance_wake_voice(0.1, true, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "waking animation precedes spoken introduction")
	encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
	encounter._advance_wake_voice(0.1, true, false)
	var first_turn := encounter._client.active_turn_id()
	test.assert_true(not first_turn.is_empty(), "awake encounter requests the greeting")
	test.assert_equal(encounter._client.send_audio_frame(PackedByteArray([0, 0])), false, "activation never opens microphone input")
	test.assert_equal(encounter.state.intro_delivered, false, "queued speech is not delivered text or heard audio")
	encounter.state.advance(0.1, true, false)
	encounter._on_playback_started(first_turn)
	test.assert_equal(encounter.state.intro_visible, true, "own greeting playback preserves text fallback")
	test.assert_equal(encounter.state.intro_delivered, false, "playback does not mark text delivered")
	encounter._on_playback_stopped(first_turn)
	encounter._client.interrupt_active_turn()
	encounter._advance_wake_voice(0.1, true, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "accepted greeting does not replay")
	_free_wake_fixture(encounter)

	encounter = _wake_fixture()
	encounter.state.restore({"activated": true})
	encounter._advance_wake_voice(0.1, true, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "restored awake save never queues first-wake speech")
	_free_wake_fixture(encounter)

	encounter = _wake_fixture(["game_events_v1"])
	encounter.activate_lilt()
	encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
	encounter._advance_wake_voice(0.1, true, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "old backend leaves introduction as text")
	encounter._advance_wake_voice(LiltEncounter.WAKE_VOICE_WAIT_SECONDS, true, false)
	test.assert_equal(encounter._wake_voice_pending, false, "offline invitation expires")
	_free_wake_fixture(encounter)

	for interruption: String in ["walk_away", "talk"]:
		encounter = _wake_fixture()
		encounter.activate_lilt()
		encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
		encounter._advance_wake_voice(0.1, interruption != "walk_away", interruption == "talk")
		encounter._advance_wake_voice(0.1, true, false)
		test.assert_equal(encounter._client.active_turn_id(), "", "cancelled invitation stays cancelled: " + interruption)
		_free_wake_fixture(encounter)

	encounter = _wake_fixture()
	encounter.activate_lilt()
	encounter._advance_wake_voice(0.1, false, false)
	encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
	encounter._advance_wake_voice(0.1, true, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "leaving during wake animation cancels invitation")
	_free_wake_fixture(encounter)

	encounter = _wake_fixture()
	encounter.activate_lilt()
	encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
	encounter._advance_wake_voice(0.1, true, false)
	encounter._advance_wake_voice(0.1, false, false)
	test.assert_equal(encounter._client.active_turn_id(), "", "leaving retires owned active greeting")
	_free_wake_fixture(encounter)

	encounter = _wake_fixture()
	encounter.activate_lilt()
	encounter.state.advance(LiltEncounterState.WAKE_SECONDS, true, false)
	encounter._advance_wake_voice(0.1, true, false)
	encounter._client.start_turn_with_id("player-priority", {})
	encounter._on_capture_started()
	test.assert_equal(encounter._client.active_turn_id(), "player-priority", "wake cancellation preserves newer player turn")
	_free_wake_fixture(encounter)
