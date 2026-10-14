extends RefCounted

const CLIENT_PATH: String = "res://scripts/companion/companion_client.gd"


class RecordingClient:
	extends CompanionClient

	var control_frames: Array[Dictionary] = []
	var binary_frames: Array[PackedByteArray] = []


	func _send_control(payload: Dictionary) -> Error:
		control_frames.append(payload.duplicate(true))
		return OK


	func _send_binary(payload: PackedByteArray) -> Error:
		binary_frames.append(payload.duplicate())
		return OK


class FailingClient:
	extends RecordingClient

	func _send_control(_payload: Dictionary) -> Error:
		return ERR_UNAVAILABLE


class HeaderRecordingClient:
	extends CompanionClient

	var observed_endpoint: String = ""
	var observed_headers: PackedStringArray = PackedStringArray()

	func _connect_socket(socket: WebSocketPeer) -> Error:
		observed_endpoint = endpoint
		observed_headers = socket.handshake_headers.duplicate()
		return OK


func run(test: RefCounted) -> bool:
	if not ResourceLoader.exists(CLIENT_PATH):
		test.assert_true(false, "companion client exists")
		return true
	_run_save_override_cases(test)
	_run_game_event_cases(test)
	_run_wake_event_cases(test)
	_run_handshake_and_turn_cases(test)
	_run_bearer_header_cases(test)
	_run_rejected_reconfiguration_cases(test)
	_run_failed_handshake_case(test)
	_run_unsupported_protocol_case(test)
	_run_stale_signal_case(test)
	_run_nonrecoverable_error_case(test)
	_run_binary_signal_case(test)
	_run_annotation_sanitization_case(test)
	_run_outbound_bound_cases(test)
	_run_request_history_bound_case(test)
	_run_counted_protocol_failure_rotation_case(test)
	_run_turn_history_retirement_case(test)
	_run_reconnect_cases(test)
	return true


func _run_nonrecoverable_error_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	client.start_turn_with_id("turn-failed", {})
	client.accept_server_text(JSON.stringify({
		"type": "error",
		"turn_id": "turn-failed",
		"code": "provider_failure",
		"message": "failed",
		"recoverable": false,
	}))
	test.assert_equal(
		client.active_turn_id(),
		"",
		"a non-recoverable server error retires the active turn",
	)
	test.assert_equal(
		client.send_audio_frame(PackedByteArray([0, 0])),
		false,
		"a terminally failed turn cannot accept more input",
	)
	client.free()


func _run_bearer_header_cases(test: RefCounted) -> void:
	var client := HeaderRecordingClient.new()
	client.auto_connect = false
	client.configure(
		"phase_2_prototype",
		"ws://127.0.0.1:8000/api/v1/game-companion/ws",
		"local-test-token",
	)
	test.assert_equal(client.connect_now(), OK, "configured Bearer token starts transport")
	test.assert_equal(
		client.observed_headers,
		PackedStringArray(["Authorization: Bearer local-test-token"]),
		"WebSocket handshake carries the token only in the Authorization header",
	)
	test.assert_equal(
		client.observed_endpoint,
		"ws://127.0.0.1:8000/api/v1/game-companion/ws",
		"credentials never return to the WebSocket query string",
	)
	client.disconnect_now()
	client.configure(
		"phase_2_prototype",
		"ws://127.0.0.1:8000/api/v1/game-companion/ws",
		"bad\r\ntoken",
	)
	test.assert_equal(
		client.connect_now(),
		ERR_INVALID_PARAMETER,
		"header-injection characters fail closed before transport",
	)
	test.assert_equal(
		client.observed_headers,
		PackedStringArray(["Authorization: Bearer local-test-token"]),
		"invalid credentials are never passed to the socket seam",
	)
	client.free()

	for loopback_endpoint: String in [
		"ws://localhost:8000/api/v1/game-companion/ws",
		"ws://127.1.2.3:8000/api/v1/game-companion/ws",
		"ws://[::1]:8000/api/v1/game-companion/ws",
		"WS://127.0.0.1:8000/api/v1/game-companion/ws",
	]:
		var loopback := HeaderRecordingClient.new()
		loopback.auto_connect = false
		loopback.configure("phase_2_prototype", loopback_endpoint, "loopback-token")
		test.assert_equal(
			loopback.connect_now(),
			OK,
			"cleartext loopback endpoint remains available: %s" % loopback_endpoint,
		)
		loopback.free()

	var remote_tls := HeaderRecordingClient.new()
	remote_tls.auto_connect = false
	remote_tls.configure(
		"phase_2_prototype",
		"wss://dograh.example/api/v1/game-companion/ws",
		"remote-tls-token",
	)
	test.assert_equal(remote_tls.connect_now(), OK, "remote TLS endpoint remains available")
	test.assert_equal(
		remote_tls.observed_headers,
		PackedStringArray(["Authorization: Bearer remote-tls-token"]),
		"remote TLS endpoint receives the Bearer header",
	)
	remote_tls.free()

	for malformed_endpoint: String in [
		"ws://127.0.0.1.evil.example/api/v1/game-companion/ws",
		"ws://localhost.evil.example/api/v1/game-companion/ws",
		"ws://2130706433/api/v1/game-companion/ws",
		"ws://0x7f000001/api/v1/game-companion/ws",
		"ws://0127.0.0.1/api/v1/game-companion/ws",
		"wss://127.0.0.1@evil.example/api/v1/game-companion/ws",
		"ws://127.0.0.1?ignore@evil.example",
		"wss://127.0.0.1?ignore@evil.example",
		"wss://:8000/api/v1/game-companion/ws",
		"ws://.127.0.0.1/api/v1/game-companion/ws",
		"ws://127.0.0.1./api/v1/game-companion/ws",
		" ws://127.0.0.1:8000/api/v1/game-companion/ws",
		"ws://127.0.0.1:8000/api/v1/game-companion/ws ",
		"ws://127.0.0.1:8000/ws\r\nOrigin: evil.example",
	]:
		test.assert_equal(
			CompanionClient._endpoint_allows_authorization(malformed_endpoint),
			false,
			"malformed or deceptive endpoint fails closed: %s" % malformed_endpoint,
		)

	for insecure_endpoint: String in [
		"ws://dograh.example/api/v1/game-companion/ws",
		"ws://localhost@dograh.example/api/v1/game-companion/ws",
	]:
		var insecure := HeaderRecordingClient.new()
		insecure.auto_connect = false
		insecure.configure("phase_2_prototype", insecure_endpoint, "secret-token")
		test.assert_equal(
			insecure.connect_now(),
			ERR_INVALID_PARAMETER,
			"insecure remote endpoint fails closed: %s" % insecure_endpoint,
		)
		test.assert_equal(
			insecure.observed_endpoint,
			"",
			"insecure endpoint never reaches the transport seam",
		)
		insecure.free()

	for malformed_value: String in [
		"bad%stoken" % String.chr(0x01),
		"bad\ttoken",
		"bad%stoken" % String.chr(0x7f),
		"tokÃ©n",
	]:
		var malformed_token := HeaderRecordingClient.new()
		malformed_token.auto_connect = false
		malformed_token.configure(
			"phase_2_prototype",
			"ws://127.0.0.1:8000/api/v1/game-companion/ws",
			malformed_value,
		)
		test.assert_equal(
			malformed_token.connect_now(),
			ERR_INVALID_PARAMETER,
			"non-ASCII or control character in Bearer token fails closed",
		)
		test.assert_equal(
			malformed_token.observed_endpoint,
			"",
			"malformed Bearer token never reaches the transport seam",
		)
		malformed_token.free()


func _run_rejected_reconfiguration_cases(test: RefCounted) -> void:
	for invalid_configuration: Dictionary in [
		{
			"endpoint": "ws://dograh.example/api/v1/game-companion/ws",
			"token": "valid-token",
			"error": &"insecure_endpoint",
		},
		{
			"endpoint": CompanionClient.DEFAULT_URL,
			"token": "bad\ttoken",
			"error": &"authentication_required",
		},
	]:
		var client := RecordingClient.new()
		client.auto_connect = false
		client.configure("phase_2_prototype", CompanionClient.DEFAULT_URL, "valid-token")
		client.begin_handshake()
		client.accept_server_text(JSON.stringify(_hello_ack()))
		client.start_turn_with_id("turn-before-reconfigure", {})
		client._socket = WebSocketPeer.new()
		var interrupted: Array[String] = []
		var errors: Array[Dictionary] = []
		client.turn_interrupted.connect(
			func(turn_id: String) -> void: interrupted.append(turn_id)
		)
		client.companion_error.connect(
			func(code: StringName, _message: String, recoverable: bool) -> void:
				errors.append({"code": code, "recoverable": recoverable})
		)
		client.configure(
			"phase_2_prototype",
			String(invalid_configuration.endpoint),
			String(invalid_configuration.token),
		)
		test.assert_equal(
			client.connect_now(),
			ERR_INVALID_PARAMETER,
			"invalid reconfiguration fails before reconnect",
		)
		test.assert_equal(
			client.connection_state_label(),
			&"offline",
			"invalid reconfiguration reports the transport offline",
		)
		test.assert_equal(
			client._socket,
			null,
			"invalid reconfiguration retires the prior socket",
		)
		test.assert_equal(
			client.active_turn_id(),
			"",
			"invalid reconfiguration retires prior turn authority",
		)
		test.assert_equal(
			interrupted,
			["turn-before-reconfigure"],
			"invalid reconfiguration interrupts the prior turn exactly once",
		)
		test.assert_equal(
			errors,
			[{"code": invalid_configuration.error, "recoverable": true}],
			"invalid reconfiguration emits its recoverable typed error",
		)
		client.free()


func _run_handshake_and_turn_cases(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var negotiated_input_rates: Array[int] = []
	test.assert_true(
		client.has_signal("input_sample_rate_changed"),
		"client exposes the negotiated input sample rate",
	)
	if client.has_signal("input_sample_rate_changed"):
		client.connect(
			"input_sample_rate_changed",
			func(sample_rate: int) -> void: negotiated_input_rates.append(sample_rate),
		)
	client.configure("phase_2_prototype")
	test.assert_equal(
		client.start_turn_with_id("too-early", {}),
		false,
		"turns cannot begin before hello_ack",
	)
	test.assert_equal(client.begin_handshake(), OK, "handshake sends hello")
	test.assert_equal(client.control_frames.size(), 1, "one hello frame is sent")
	test.assert_equal(client.control_frames[0].type, "hello", "hello is sent first")
	test.assert_equal(
		client.connection_state_label(), &"handshaking", "client awaits hello_ack"
	)

	client.accept_server_text(JSON.stringify(_hello_ack(24000)))
	test.assert_equal(client.connection_state_label(), &"ready", "hello_ack readies client")
	test.assert_equal(
		negotiated_input_rates,
		[24000],
		"hello_ack publishes the negotiated capture rate before turns open",
	)
	test.assert_equal(
		client.start_turn_with_id("turn-1", {"authority_mode": "ship"}),
		true,
		"a ready client sends turn_start",
	)
	test.assert_equal(client.control_frames[-1].type, "turn_start", "turn_start is sent")
	test.assert_equal(
		client.send_audio_frame(PackedByteArray([0, 0, 1, 0])),
		true,
		"ordered PCM is sent during an input turn",
	)
	test.assert_equal(client.binary_frames.size(), 1, "one PCM frame is sent")
	test.assert_equal(client.end_turn(), true, "turn_end closes input")
	test.assert_equal(client.control_frames[-1].type, "turn_end", "turn_end is sent")
	client.free()


func _run_unsupported_protocol_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var errors: Array[Dictionary] = []
	client.companion_error.connect(
		func(code: StringName, _message: String, recoverable: bool) -> void:
			errors.append({"code": code, "recoverable": recoverable})
	)
	client.begin_handshake()
	var unsupported := _hello_ack()
	unsupported.protocol_version = CompanionProtocol.PROTOCOL_VERSION + 1
	test.assert_equal(
		client.accept_server_text(JSON.stringify(unsupported)),
		false,
		"unsupported protocol acknowledgement is rejected",
	)
	test.assert_equal(
		client.connection_state_label(),
		&"offline",
		"unsupported protocol retires the incompatible transport",
	)
	test.assert_equal(
		client.next_reconnect_at_msec(),
		-1,
		"unsupported protocol never schedules an automatic reconnect loop",
	)
	test.assert_equal(
		errors[-1],
		{"code": &"unsupported_protocol", "recoverable": false},
		"unsupported protocol remains a visible nonrecoverable failure",
	)
	client.free()


func _run_stale_signal_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var captions: Array[String] = []
	var errors: Array[StringName] = []
	client.caption_received.connect(
		func(_turn_id: String, _speaker: StringName, text: String, _final: bool) -> void:
			captions.append(text)
	)
	client.companion_error.connect(
		func(code: StringName, _message: String, _recoverable: bool) -> void:
			errors.append(code)
	)
	client.configure("phase_2_prototype")
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	client.start_turn_with_id("turn-new", {})
	client.accept_server_text(
		JSON.stringify(
			{
				"type": "caption",
				"turn_id": "turn-old",
				"speaker": "assistant",
				"text": "stale",
				"final": true,
			}
		)
	)
	client.accept_server_text(
		JSON.stringify(
			{
				"type": "caption",
				"turn_id": "turn-new",
				"speaker": "assistant",
				"text": "current",
				"final": true,
			}
		)
	)
	test.assert_equal(captions, ["current"], "only the current turn caption is signalled")
	client.interrupt_active_turn()
	client.accept_server_text(
		JSON.stringify(
			{
				"type": "caption",
				"turn_id": "turn-new",
				"speaker": "assistant",
				"text": "retired",
				"final": true,
			}
		)
	)
	test.assert_equal(captions, ["current"], "retired-turn captions stay suppressed")
	test.assert_equal(errors, [], "retired-turn output does not emit a protocol error")
	client.free()


func _run_failed_handshake_case(test: RefCounted) -> void:
	var client := FailingClient.new()
	test.assert_equal(
		client.begin_handshake(), ERR_UNAVAILABLE, "a failed hello send is reported"
	)
	test.assert_equal(
		client.connection_state_label(),
		&"offline",
		"a failed hello send rolls back the handshaking state",
	)
	client.free()


func _run_binary_signal_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var received_frames: Array[PackedByteArray] = []
	client.audio_frame_received.connect(
		func(_turn_id: String, frame: PackedByteArray) -> void:
			received_frames.append(frame.duplicate())
	)
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	client.start_turn_with_id("turn-audio", {})
	client.accept_server_text(
		JSON.stringify(
			{
				"type": "audio_start",
				"turn_id": "turn-audio",
				"sample_rate": 24000,
				"channels": 1,
				"format": "pcm_s16le",
			}
		)
	)
	var pcm := PackedByteArray([1, 0, 2, 0])
	test.assert_equal(client.accept_server_binary(pcm), true, "ordered PCM is delivered")
	test.assert_equal(received_frames, [pcm], "binary delivery preserves PCM bytes")
	client.free()


func _run_annotation_sanitization_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var proposals: Array[Dictionary] = []
	client.annotation_proposed.connect(
		func(_turn_id: String, proposal: Dictionary) -> void:
			proposals.append(proposal)
	)
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	client.start_turn_with_id("turn-annotation", {})
	client.accept_server_text(
		JSON.stringify(
			{
				"type": "annotation_proposal",
				"turn_id": "turn-annotation",
				"proposal_id": "proposal-1",
				"source_event_ids": ["event-1"],
				"summary": "Grounded analysis",
				"untrusted_future_field": "not forwarded",
			}
		)
	)
	test.assert_equal(proposals.size(), 1, "valid annotation proposal is signalled")
	test.assert_equal(
		proposals[0].has("untrusted_future_field"),
		false,
		"unknown protocol fields do not cross the gameplay boundary",
	)
	test.assert_equal(proposals[0].tags, [], "omitted optional tags use the canonical default")
	client.free()


func _run_outbound_bound_cases(test: RefCounted) -> void:
	var client := RecordingClient.new()
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(
		client.start_turn_with_id(
			"oversized-context",
			{"padding": "x".repeat(CompanionClient.MAX_CONTEXT_BYTES)},
		),
		false,
		"turn context is bounded to the server contract",
	)
	test.assert_equal(
		client.start_turn_with_id("bounded-audio", {}), true, "bounded turn starts"
	)
	test.assert_equal(client.start_turn_with_id("replacement", {}), true, "new turn replaces old")
	var frame_count_before_reuse := client.control_frames.size()
	test.assert_equal(
		client.start_turn_with_id("bounded-audio", {}),
		false,
		"a retired turn identifier is rejected before transport writes",
	)
	test.assert_equal(
		client.control_frames.size(),
		frame_count_before_reuse,
		"rejected turn reuse emits no interrupt or turn_start",
	)
	client.interrupt_active_turn()
	test.assert_equal(
		client.start_turn_with_id("audio-limit", {}), true, "audio limit turn starts"
	)
	var maximum_frame := PackedByteArray()
	maximum_frame.resize(CompanionProtocol.MAX_BINARY_FRAME_BYTES)
	var all_within_bound_sent := true
	var bounded_frame_count := floori(
		float(CompanionClient.MAX_TURN_AUDIO_BYTES)
		/ float(CompanionProtocol.MAX_BINARY_FRAME_BYTES)
	)
	for _index: int in range(bounded_frame_count):
		all_within_bound_sent = client.send_audio_frame(maximum_frame) and all_within_bound_sent
	test.assert_equal(all_within_bound_sent, true, "audio within the turn bound is sent")
	test.assert_equal(
		client.send_audio_frame(PackedByteArray([0, 0])),
		false,
		"audio beyond the cumulative turn bound is rejected",
	)
	client.free()


func _run_request_history_bound_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	client.start_turn_with_id("turn-bounded-requests", {})
	for index in CompanionClient.MAX_REQUEST_IDS_PER_TURN:
		var accepted := client.accept_server_text(JSON.stringify({
			"type": "tool_call",
			"turn_id": "turn-bounded-requests",
			"call_id": "bounded-call-%d" % index,
			"name": "request_assisted_landing",
			"arguments": {},
		}))
		test.assert_true(accepted, "bounded request history accepts in-budget call %d" % index)
	test.assert_equal(
		client.accept_server_text(JSON.stringify({
			"type": "memory_query",
			"turn_id": "turn-bounded-requests",
			"query_id": "request-overflow",
			"name": "recent_journal_items",
			"arguments": {},
		})),
		false,
		"request history rejects the first aggregate overflow",
	)
	test.assert_equal(
		client.connection_state_label(),
		&"reconnect_wait",
		"request history exhaustion rotates to a fresh bounded session",
	)
	client.free()


func _run_reconnect_cases(test: RefCounted) -> void:
	var client := RecordingClient.new()
	client.configure("phase_2_prototype")
	client.notify_transport_closed(1000)
	test.assert_equal(
		client.connection_state_label(), &"reconnect_wait", "disconnect schedules reconnect"
	)
	test.assert_equal(
		client.next_reconnect_at_msec(),
		1500,
		"first reconnect waits the bounded base delay",
	)
	test.assert_equal(client.reconnect_due(1499), false, "reconnect does not fire early")
	test.assert_equal(client.reconnect_due(1500), true, "reconnect fires at its deadline")

	for attempt: int in range(12):
		client.notify_transport_closed(2000 + attempt)
	test.assert_true(
		client.reconnect_delay_msec() <= CompanionClient.MAX_RECONNECT_DELAY_MSEC,
		"exponential reconnect delay remains bounded",
	)
	client.free()

	var presentation_client := RecordingClient.new()
	var statuses: Array[StringName] = []
	presentation_client.status_changed.connect(
		func(status: StringName) -> void: statuses.append(status)
	)
	presentation_client.notify_transport_closed(3000)
	for attempt: int in range(CompanionClient.QUIET_RECONNECT_AFTER_ATTEMPTS + 1):
		presentation_client.begin_handshake()
		presentation_client.notify_transport_closed(4000 + attempt)
	test.assert_equal(
		statuses,
		[&"offline", &"connecting", &"offline", &"connecting", &"offline_quiet"],
		"prolonged retry presentation becomes stable after the visible retry budget",
	)
	test.assert_equal(
		presentation_client.connection_state_label(),
		&"reconnect_wait",
		"quiet presentation does not stop the internal reconnect state machine",
	)
	presentation_client.begin_handshake()
	presentation_client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(statuses[-1], &"idle", "successful recovery exits quiet offline state")
	presentation_client.free()


func _run_turn_history_retirement_case(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var errors: Array[Dictionary] = []
	client.companion_error.connect(
		func(code: StringName, _message: String, recoverable: bool) -> void:
			errors.append({"code": code, "recoverable": recoverable})
	)
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	for index: int in range(CompanionProtocol.MAX_SEEN_TURN_IDS):
		test.assert_equal(
			client.start_turn_with_id("history-turn-%d" % index, {}),
			true,
			"client accepts turns within the protocol history bound",
		)
		client.interrupt_active_turn()
	test.assert_equal(
		client.start_turn_with_id("history-overflow", {}),
		false,
		"history overflow refuses an ambiguous turn identifier",
	)
	test.assert_equal(
		client.connection_state_label(),
		&"reconnect_wait",
		"history exhaustion rotates the connection instead of reusing identifiers",
	)
	test.assert_equal(
		errors[-1],
		{"code": &"turn_history_exhausted", "recoverable": true},
		"history exhaustion is a typed recoverable rotation",
	)
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(
		client.start_turn_with_id("history-after-rotation", {}),
		true,
		"a fresh connection accepts turns after bounded history rotation",
	)
	client.free()


func _run_counted_protocol_failure_rotation_case(test: RefCounted) -> void:
	var malformed_client := RecordingClient.new()
	var malformed_errors: Array[Dictionary] = []
	malformed_client.companion_error.connect(
		func(code: StringName, _message: String, recoverable: bool) -> void:
			malformed_errors.append({"code": code, "recoverable": recoverable})
	)
	malformed_client.begin_handshake()
	malformed_client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(malformed_client.accept_server_text("{invalid"), false, "first malformed frame is rejected")
	test.assert_equal(malformed_client.accept_server_text("{invalid"), false, "second malformed frame is rejected")
	test.assert_equal(
		malformed_client.connection_state_label(),
		&"reconnect_wait",
		"counted recoverable faults rotate through normal reconnect",
	)
	test.assert_equal(
		malformed_client.reconnect_delay_msec(),
		CompanionClient.BASE_RECONNECT_DELAY_MSEC,
		"the first counted-fault rotation uses base backoff",
	)
	test.assert_equal(
		malformed_errors,
		[
			{"code": &"invalid_json", "recoverable": true},
			{"code": &"invalid_json", "recoverable": true},
		],
		"counted protocol faults remain recoverable when the transport rotates",
	)
	malformed_client.begin_handshake()
	malformed_client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(malformed_client.accept_server_text("{invalid"), false, "a fresh session counts its first malformed frame")
	test.assert_equal(malformed_client.accept_server_text("{invalid"), false, "a fresh session rotates after its second malformed frame")
	test.assert_equal(
		malformed_client.reconnect_delay_msec(),
		CompanionClient.BASE_RECONNECT_DELAY_MSEC * 2,
		"repeated counted-fault rotations retain bounded exponential backoff",
	)
	malformed_client.begin_handshake()
	malformed_client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_true(
		malformed_client.start_turn_with_id("turn-healthy-after-rotation", {}),
		"a session can become healthy after protocol rotation",
	)
	test.assert_true(malformed_client.end_turn(), "the healthy session closes its input")
	test.assert_true(
		malformed_client.accept_server_text(JSON.stringify({
			"type": "state",
			"turn_id": "turn-healthy-after-rotation",
			"state": "idle",
		})),
		"the server acknowledges a healthy completed turn",
	)
	malformed_client.notify_transport_closed(4000)
	test.assert_equal(
		malformed_client.reconnect_delay_msec(),
		CompanionClient.BASE_RECONNECT_DELAY_MSEC,
		"a healthy completed turn restores ordinary reconnect backoff",
	)
	malformed_client.begin_handshake()
	malformed_client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(
		malformed_client.start_turn_with_id("turn-after-malformed-rotation", {}),
		true,
		"a fresh connection accepts turns after counted-fault rotation",
	)
	malformed_client.free()

	var duplicate_client := RecordingClient.new()
	duplicate_client.begin_handshake()
	duplicate_client.accept_server_text(JSON.stringify(_hello_ack()))
	duplicate_client.start_turn_with_id("turn-duplicate", {})
	var tool_call := {
		"type": "tool_call",
		"turn_id": "turn-duplicate",
		"call_id": "call-duplicate",
		"name": "request_assisted_landing",
		"arguments": {},
	}
	test.assert_equal(duplicate_client.accept_server_text(JSON.stringify(tool_call)), true, "first tool call is accepted")
	test.assert_equal(duplicate_client.accept_server_text(JSON.stringify(tool_call)), false, "first duplicate is rejected")
	test.assert_equal(duplicate_client.accept_server_text(JSON.stringify(tool_call)), false, "second duplicate is rejected")
	test.assert_equal(
		duplicate_client.connection_state_label(),
		&"reconnect_wait",
		"duplicate identifiers accumulate toward recoverable rotation",
	)
	duplicate_client.free()


func _hello_ack(sample_rate: int = 16000) -> Dictionary:
	return {
		"type": "hello_ack",
		"protocol_version": 1,
		"session_id": "session-1",
		"companion": "Aster",
		"audio": {
			"sample_rate": sample_rate,
			"channels": 1,
			"format": "pcm_s16le",
		},
	}


func _run_save_override_cases(test: RefCounted) -> void:
	var had_override := OS.has_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE)
	var previous_override := OS.get_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE)
	OS.unset_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE)
	var ordinary := RecordingClient.new()
	ordinary.auto_connect = false
	var ordinary_parent := Node.new()
	ordinary_parent.process_mode = Node.PROCESS_MODE_DISABLED
	(Engine.get_main_loop() as SceneTree).root.add_child(ordinary_parent)
	ordinary_parent.add_child(ordinary)
	test.assert_equal(ordinary.save_id, CompanionClient.DEFAULT_SAVE_ID, "absent review override keeps default identity")
	ordinary.configure("custom_save")
	test.assert_equal(ordinary.save_id, "custom_save", "absent override keeps explicit configuration")
	ordinary_parent.free()

	OS.set_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE, "fieldwork_review_0123456789abcdef")
	var review := RecordingClient.new()
	review.auto_connect = false
	var review_parent := Node.new()
	review_parent.process_mode = Node.PROCESS_MODE_DISABLED
	(Engine.get_main_loop() as SceneTree).root.add_child(review_parent)
	review_parent.add_child(review)
	test.assert_equal(review.save_id, "fieldwork_review_0123456789abcdef", "review identity applies before connection")
	review.configure("custom_save")
	test.assert_equal(review.save_id, "fieldwork_review_0123456789abcdef", "review identity survives later configuration")
	test.assert_equal(review.begin_handshake(), OK, "valid review identity starts hello")
	test.assert_equal(review.control_frames.back().save_id, review.save_id, "backend hello uses review identity")
	var context := CompanionSaveContext.new()
	test.assert_true(context.configure(review.save_id), "local memory accepts review identity")
	test.assert_equal(context.companion_directory(), "user://saves/%s/companion" % review.save_id, "local memory follows backend identity")
	review_parent.free()

	for invalid_override: String in ["", "../shared", "x".repeat(65)]:
		OS.set_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE, invalid_override)
		var rejected := RecordingClient.new()
		rejected.auto_connect = false
		var rejected_parent := Node.new()
		rejected_parent.process_mode = Node.PROCESS_MODE_DISABLED
		(Engine.get_main_loop() as SceneTree).root.add_child(rejected_parent)
		rejected_parent.add_child(rejected)
		test.assert_equal(rejected.save_id, "", "invalid override cannot fall back to shared save")
		test.assert_equal(rejected.connect_now(), ERR_INVALID_PARAMETER, "invalid override blocks transport")
		test.assert_equal(rejected.begin_handshake(), ERR_INVALID_PARAMETER, "invalid override cannot send hello")
		test.assert_equal(rejected.control_frames.size(), 0, "invalid override sends no backend identity")
		rejected_parent.free()
	if had_override:
		OS.set_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE, previous_override)
	else:
		OS.unset_environment(CompanionClient.SAVE_ID_ENVIRONMENT_VARIABLE)


func _run_game_event_cases(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var context := {"discovery": {"station_sample_state": "receiver_ready", "receiver_owned": true}}
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(client.start_game_event(&"station_assay_complete", context), "", "older server cannot receive an event")
	client.disconnect_now()
	client.begin_handshake()
	var ack := _hello_ack()
	ack["capabilities"] = ["game_events_v1"]
	client.accept_server_text(JSON.stringify(ack))
	test.assert_equal(client.start_game_event(&"station_assay_complete", {}), "", "unearned assay cannot initiate Aster")
	test.assert_equal(client.start_game_event(&"unknown", context), "", "only allowlisted events can initiate Aster")
	var turn_id := client.start_game_event(&"station_assay_complete", context)
	test.assert_true(not turn_id.is_empty(), "earned event starts without microphone input")
	test.assert_equal(client.control_frames.back().type, "game_event", "explicit event frame is sent")
	test.assert_equal(client.send_audio_frame(PackedByteArray([0, 0])), false, "event cannot transmit microphone audio")
	test.assert_equal(client.end_turn(), false, "event does not fake an audio turn end")
	test.assert_equal(client.start_game_event(&"station_assay_complete", context), "", "an event never preempts active speech")
	test.assert_true(client.accept_server_text(JSON.stringify({
		"type": "caption", "turn_id": turn_id, "speaker": "assistant",
		"text": "Did you notice the repetition?", "final": true,
	})), "actual opening caption reaches the game")
	test.assert_true(client.start_turn_with_id("player-reply", {}), "player reply can interrupt Aster opening")
	test.assert_equal(client.control_frames.back().context.recent_station_opening.caption_received, "Did you notice the repetition?", "reply receives actual opening rather than planned script")
	test.assert_true("interrupted" in String(client.control_frames.back().context.recent_station_opening.delivery), "caption receipt does not claim audible completion")
	test.assert_equal(client.send_audio_frame(PackedByteArray([0, 0])), true, "normal player voice remains available")
	test.assert_true(client.start_turn_with_id("second-reply", {}), "another reply starts normally")
	test.assert_true(not client.control_frames.back().context.has("recent_station_opening"), "opening is consumed by the first reply")
	client._game_event_caption = "An expired opening"
	client._game_event_caption_msec = Time.get_ticks_msec() - 120001
	test.assert_true(client.start_turn_with_id("later-reply", {}), "later reply starts normally")
	test.assert_true(not client.control_frames.back().context.has("recent_station_opening"), "stale invitation caption expires")
	client.disconnect_now()
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(client.can_start_game_event(), false, "reconnecting resets event capability")
	client.free()


func _run_wake_event_cases(test: RefCounted) -> void:
	var client := RecordingClient.new()
	var context := {"companion_wake": {
		"activated": true, "phase": "awake", "first_activation_this_session": true,
	}}
	client.begin_handshake()
	var ack := _hello_ack()
	ack["capabilities"] = ["game_events_v1"]
	client.accept_server_text(JSON.stringify(ack))
	test.assert_equal(client.start_game_event(&"aster_first_wake", context), "", "station-only service keeps wake text-only")
	client.disconnect_now()
	client.begin_handshake()
	ack["capabilities"] = ["game_events_v1", "aster_wake_v1"]
	client.accept_server_text(JSON.stringify(ack))
	for invalid: Dictionary in [
		{}, {"companion_wake": "not facts"},
		{"companion_wake": {"activated": false, "phase": "awake", "first_activation_this_session": true}},
		{"companion_wake": {"activated": true, "phase": "waking", "first_activation_this_session": true}},
		{"companion_wake": {"activated": true, "phase": "awake", "first_activation_this_session": false}},
		{"companion_wake": {"activated": 1, "phase": "awake", "first_activation_this_session": true}},
		{"companion_wake": {"activated": true, "phase": "awake", "first_activation_this_session": "true"}},
	]:
		test.assert_equal(client.start_game_event(&"aster_first_wake", invalid), "", "unearned first wake rejected")
	var turn_id := client.start_game_event(&"aster_first_wake", context)
	test.assert_true(not turn_id.is_empty(), "fresh awake event starts")
	test.assert_equal(client.control_frames.back().event, "aster_first_wake", "wake event is explicit")
	test.assert_true(not client.control_frames.back().context.has("discovery"), "wake does not invent an assay")
	test.assert_equal(client.send_audio_frame(PackedByteArray([0, 0])), false, "wake sends no microphone audio")
	test.assert_equal(client.end_turn(), false, "wake needs no pretend microphone end")
	test.assert_equal(client.start_game_event(&"aster_first_wake", context), "", "wake cannot interrupt an active turn")
	client.accept_server_text(JSON.stringify({
		"type": "caption", "turn_id": turn_id, "speaker": "assistant",
		"text": "Hello. I'm Aster.", "final": true,
	}))
	test.assert_true(client.start_turn_with_id("wake-player-reply", {}), "player can interrupt the greeting")
	var reply: Dictionary = client.control_frames.back().context
	test.assert_equal(reply.recent_aster_opening.caption_received, "Hello. I'm Aster.", "reply gets actual wake caption")
	test.assert_true(not reply.has("recent_station_opening"), "wake caption cannot masquerade as an assay")
	test.assert_true("interrupted" in String(reply.recent_aster_opening.delivery), "caption is not proof of heard speech")
	test.assert_true(client.start_turn_with_id("wake-second-reply", {}), "second player reply works")
	test.assert_true(not client.control_frames.back().context.has("recent_aster_opening"), "wake caption consumed once")
	client.disconnect_now()
	client.begin_handshake()
	client.accept_server_text(JSON.stringify(_hello_ack()))
	test.assert_equal(client.can_start_game_event(&"aster_first_wake"), false, "wake capability resets on reconnect")
	client.free()
