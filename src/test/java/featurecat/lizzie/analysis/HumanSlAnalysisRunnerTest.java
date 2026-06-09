package featurecat.lizzie.analysis;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import featurecat.lizzie.Config;
import featurecat.lizzie.Lizzie;
import featurecat.lizzie.rules.Board;
import featurecat.lizzie.rules.BoardData;
import featurecat.lizzie.rules.BoardHistoryList;
import featurecat.lizzie.rules.Stone;
import featurecat.lizzie.rules.Zobrist;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PipedInputStream;
import java.io.PipedOutputStream;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.jupiter.api.Test;

class HumanSlAnalysisRunnerTest {
  private static final int BOARD_SIZE = 3;
  private static final int BOARD_AREA = BOARD_SIZE * BOARD_SIZE;

  @Test
  void buildHumanSlCommand_replacesGtpAndAddsHumanModel() {
    List<String> command =
        HumanSlAnalysisRunner.buildHumanSlCommand(
            "katago gtp -model main.bin.gz -config gtp.cfg", Path.of("human.bin.gz"));

    assertEquals("analysis", command.get(1));
    assertTrue(command.contains("-human-model"));
    assertTrue(command.get(command.indexOf("-human-model") + 1).endsWith("human.bin.gz"));
  }

  @Test
  void buildHumanSlRequest_includesPolicyProfileAndPositionSettings() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      BoardHistoryList history = new BoardHistoryList(BoardData.empty(BOARD_SIZE, BOARD_SIZE));
      boardWithHistory(history);

      JSONObject request =
          HumanSlAnalysisRunner.buildHumanSlRequest(
              "humansl-1", history.getCurrentHistoryNode(), "rank_1d");

      assertEquals("humansl-1", request.getString("id"));
      assertTrue(request.getBoolean("includePolicy"));
      assertEquals(1, request.getInt("maxVisits"));
      assertEquals(
          "rank_1d", request.getJSONObject("overrideSettings").getString("humanSLProfile"));
      assertEquals(BOARD_SIZE, request.getInt("boardXSize"));
      assertEquals(BOARD_SIZE, request.getInt("boardYSize"));
    }
  }

  @Test
  void extractMoveProbability_supportsNestedPairAndNumericPolicies() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      JSONObject rootPolicy = new JSONObject().put("A3", 0.25);
      assertEquals(
          0.25, HumanSlAnalysisRunner.extractMoveProbability(rootPolicy, "A3", BOARD_SIZE));

      JSONArray pairPolicy = new JSONArray().put(new JSONArray().put("B2").put(0.125));
      assertEquals(
          0.125, HumanSlAnalysisRunner.extractMoveProbability(pairPolicy, "B2", BOARD_SIZE));

      JSONArray numericPolicy = new JSONArray();
      for (int i = 0; i < BOARD_AREA + 1; i++) {
        numericPolicy.put(0.0);
      }
      numericPolicy.put(Board.getIndex(0, 0), 0.5);
      assertEquals(
          0.5, HumanSlAnalysisRunner.extractMoveProbability(numericPolicy, "A3", BOARD_SIZE));
      assertEquals(
          1.0e-12,
          HumanSlAnalysisRunner.extractMoveProbability(
              rootPolicy.put("C1", 0.0), "C1", BOARD_SIZE));
    }
  }

  @Test
  void analyzeMainline_queriesEveryProfileAndReadsAsyncResponses() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      BoardHistoryList history = new BoardHistoryList(BoardData.empty(BOARD_SIZE, BOARD_SIZE));
      history.add(
          moveNode(stones(placement(0, 0, Stone.BLACK)), new int[] {0, 0}, Stone.BLACK, false, 1));
      boardWithHistory(history);
      FakeProcess process =
          new FakeProcess(
              request -> {
                JSONObject policy = new JSONObject().put("A3", 0.25);
                return new JSONObject()
                    .put("id", request.getString("id"))
                    .put("rootInfo", new JSONObject().put("humanPolicy", policy));
              });
      HumanSlAnalysisRunner runner =
          new HumanSlAnalysisRunner(List.of("katago", "analysis"), ignored -> process);

      HumanSlAnalysisRunner.HumanSlBatchResult batch =
          runner.analyzeMainline(
              history.getCurrentHistoryNode(),
              List.of("rank_1d", "rank_9d"),
              Duration.ofSeconds(1),
              HumanSlAnalysisRunner.CancellationToken.neverCancel());

      assertTrue(batch.isAvailable());
      assertEquals(2, batch.getResults().size());
      assertTrue(
          batch.getResults().stream()
              .allMatch(result -> result.getStatus() == HumanSlAnalysisRunner.ResultStatus.OK));
      assertEquals(2, process.sentRequests.size());
      assertEquals(
          "rank_1d",
          process
              .sentRequests
              .get(0)
              .getJSONObject("overrideSettings")
              .getString("humanSLProfile"));
      assertEquals(
          "rank_9d",
          process
              .sentRequests
              .get(1)
              .getJSONObject("overrideSettings")
              .getString("humanSLProfile"));
      runner.close();
    }
  }

  @Test
  void queryMoveProfile_missingHumanPolicyReturnsDegradedStatus() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      BoardHistoryList history = new BoardHistoryList(BoardData.empty(BOARD_SIZE, BOARD_SIZE));
      history.add(
          moveNode(stones(placement(0, 0, Stone.BLACK)), new int[] {0, 0}, Stone.BLACK, false, 1));
      boardWithHistory(history);
      FakeProcess process =
          new FakeProcess(request -> new JSONObject().put("id", request.getString("id")));
      HumanSlAnalysisRunner runner =
          new HumanSlAnalysisRunner(List.of("katago", "analysis"), ignored -> process);

      HumanSlAnalysisRunner.MoveProfileResult result =
          runner.queryMoveProfile(
              history.getCurrentHistoryNode(), "rank_1d", Duration.ofSeconds(1), null);

      assertEquals(HumanSlAnalysisRunner.ResultStatus.MISSING_HUMAN_POLICY, result.getStatus());
      assertFalse(result.getProbability().isPresent());
      runner.close();
    }
  }

  @Test
  void queryMoveProfile_timeoutReturnsTimeoutStatus() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      BoardHistoryList history = new BoardHistoryList(BoardData.empty(BOARD_SIZE, BOARD_SIZE));
      history.add(
          moveNode(stones(placement(0, 0, Stone.BLACK)), new int[] {0, 0}, Stone.BLACK, false, 1));
      boardWithHistory(history);
      FakeProcess process = new FakeProcess(request -> null);
      HumanSlAnalysisRunner runner =
          new HumanSlAnalysisRunner(List.of("katago", "analysis"), ignored -> process);

      HumanSlAnalysisRunner.MoveProfileResult result =
          runner.queryMoveProfile(
              history.getCurrentHistoryNode(), "rank_1d", Duration.ofMillis(30), null);

      assertEquals(HumanSlAnalysisRunner.ResultStatus.TIMEOUT, result.getStatus());
      runner.close();
    }
  }

  @Test
  void analyzeMainline_emptyProfilesOrMissingHistoryDegradesWithoutStartingEngine() {
    HumanSlAnalysisRunner runner =
        new HumanSlAnalysisRunner(
            List.of("katago", "analysis"),
            ignored -> {
              throw new IOException("engine should not start");
            });

    HumanSlAnalysisRunner.HumanSlBatchResult noProfiles =
        runner.analyzeMainline(
            null,
            List.of(),
            Duration.ofSeconds(1),
            HumanSlAnalysisRunner.CancellationToken.neverCancel());
    HumanSlAnalysisRunner.HumanSlBatchResult noHistory =
        runner.analyzeMainline(
            null,
            List.of("rank_1d"),
            Duration.ofSeconds(1),
            HumanSlAnalysisRunner.CancellationToken.neverCancel());

    assertFalse(noProfiles.isAvailable());
    assertEquals("No HumanSL profiles configured.", noProfiles.getUnavailableReason());
    assertFalse(noHistory.isAvailable());
    assertEquals("No board history node to analyze.", noHistory.getUnavailableReason());
    assertFalse(runner.isStarted());
  }

  @Test
  void queryMoveProfile_cancelledTokenReturnsCancelledWithoutStartingEngine() {
    HumanSlAnalysisRunner runner =
        new HumanSlAnalysisRunner(
            List.of("katago", "analysis"),
            ignored -> {
              throw new IOException("engine should not start");
            });

    HumanSlAnalysisRunner.MoveProfileResult result =
        runner.queryMoveProfile(null, "rank_1d", Duration.ofSeconds(1), () -> true);

    assertEquals(HumanSlAnalysisRunner.ResultStatus.CANCELLED, result.getStatus());
    assertEquals(-1, result.getMoveNumber());
    assertFalse(runner.isStarted());
  }

  @Test
  void argmaxPolicyMove_picksHighestProbabilityForEachPolicyShape() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      JSONObject objectPolicy = new JSONObject().put("A3", 0.1).put("B2", 0.7).put("C1", 0.2);
      assertEquals(
          "B2", HumanSlAnalysisRunner.argmaxPolicyMove(objectPolicy, BOARD_SIZE, BOARD_SIZE));

      JSONArray pairPolicy =
          new JSONArray()
              .put(new JSONArray().put("A3").put(0.2))
              .put(new JSONArray().put("C1").put(0.8));
      assertEquals(
          "C1", HumanSlAnalysisRunner.argmaxPolicyMove(pairPolicy, BOARD_SIZE, BOARD_SIZE));

      JSONArray numericPolicy = new JSONArray();
      for (int i = 0; i < BOARD_AREA + 1; i++) {
        numericPolicy.put(0.0);
      }
      numericPolicy.put(Board.getIndex(0, 0), 0.9);
      assertEquals(
          "A3", HumanSlAnalysisRunner.argmaxPolicyMove(numericPolicy, BOARD_SIZE, BOARD_SIZE));

      JSONArray passPolicy = new JSONArray();
      for (int i = 0; i < BOARD_AREA + 1; i++) {
        passPolicy.put(0.0);
      }
      passPolicy.put(BOARD_AREA, 0.95);
      assertEquals(
          "pass", HumanSlAnalysisRunner.argmaxPolicyMove(passPolicy, BOARD_SIZE, BOARD_SIZE));
    }
  }

  @Test
  void bestHumanMove_returnsArgmaxOfHumanPolicy() throws Exception {
    try (TestEnvironment env = TestEnvironment.open()) {
      BoardHistoryList history = new BoardHistoryList(BoardData.empty(BOARD_SIZE, BOARD_SIZE));
      boardWithHistory(history);
      FakeProcess process =
          new FakeProcess(
              request -> {
                JSONObject policy = new JSONObject().put("A3", 0.1).put("B2", 0.8);
                return new JSONObject()
                    .put("id", request.getString("id"))
                    .put("rootInfo", new JSONObject().put("humanPolicy", policy));
              });
      HumanSlAnalysisRunner runner =
          new HumanSlAnalysisRunner(List.of("katago", "analysis"), ignored -> process);

      java.util.Optional<String> best =
          runner.bestHumanMove(history.getCurrentHistoryNode(), "rank_3k", Duration.ofSeconds(1));

      assertTrue(best.isPresent());
      assertEquals("B2", best.get());
      runner.close();
    }
  }

  private static Board boardWithHistory(BoardHistoryList history) throws Exception {
    Board board = allocate(Board.class);
    board.startStonelist = new ArrayList<>();
    board.hasStartStone = false;
    board.setHistory(history);
    Lizzie.board = board;
    return board;
  }

  private static BoardData moveNode(
      Stone[] stones, int[] lastMove, Stone color, boolean blackToPlay, int moveNumber) {
    return BoardData.move(
        stones,
        lastMove,
        color,
        blackToPlay,
        zobrist(stones),
        moveNumber,
        new int[BOARD_AREA],
        0,
        0,
        50,
        0);
  }

  private static Stone[] stones(Placement... placements) {
    Stone[] stones = new Stone[BOARD_AREA];
    for (int i = 0; i < stones.length; i++) {
      stones[i] = Stone.EMPTY;
    }
    for (Placement placement : placements) {
      stones[Board.getIndex(placement.x, placement.y)] = placement.color;
    }
    return stones;
  }

  private static Zobrist zobrist(Stone[] stones) {
    Zobrist zobrist = new Zobrist();
    for (int x = 0; x < BOARD_SIZE; x++) {
      for (int y = 0; y < BOARD_SIZE; y++) {
        Stone stone = stones[Board.getIndex(x, y)];
        if (!stone.isEmpty()) {
          zobrist.toggleStone(x, y, stone);
        }
      }
    }
    return zobrist;
  }

  private static Placement placement(int x, int y, Stone color) {
    return new Placement(x, y, color);
  }

  @SuppressWarnings("unchecked")
  private static <T> T allocate(Class<T> type) throws Exception {
    return (T) UnsafeHolder.UNSAFE.allocateInstance(type);
  }

  private interface ResponseFactory {
    JSONObject response(JSONObject request) throws IOException;
  }

  private static final class FakeProcess extends Process {
    private final PipedInputStream stdout;
    private final PipedOutputStream stdoutWriter;
    private final OutputStream stdin;
    private final List<JSONObject> sentRequests = new ArrayList<>();
    private volatile boolean alive = true;

    private FakeProcess(ResponseFactory responseFactory) throws IOException {
      this.stdout = new PipedInputStream();
      this.stdoutWriter = new PipedOutputStream(stdout);
      this.stdin =
          new OutputStream() {
            private final ByteArrayOutputStream buffer = new ByteArrayOutputStream();

            @Override
            public synchronized void write(int b) throws IOException {
              if (b == '\n') {
                handleLine(buffer.toString(StandardCharsets.UTF_8));
                buffer.reset();
              } else {
                buffer.write(b);
              }
            }

            private void handleLine(String line) throws IOException {
              JSONObject request = new JSONObject(line);
              sentRequests.add(request);
              JSONObject response = responseFactory.response(request);
              if (response != null) {
                stdoutWriter.write((response.toString() + "\n").getBytes(StandardCharsets.UTF_8));
                stdoutWriter.flush();
              }
            }
          };
    }

    @Override
    public OutputStream getOutputStream() {
      return stdin;
    }

    @Override
    public InputStream getInputStream() {
      return stdout;
    }

    @Override
    public InputStream getErrorStream() {
      return new ByteArrayInputStream(new byte[0]);
    }

    @Override
    public int waitFor() {
      alive = false;
      return 0;
    }

    @Override
    public int exitValue() {
      return alive ? 0 : 0;
    }

    @Override
    public void destroy() {
      alive = false;
    }

    @Override
    public Process destroyForcibly() {
      destroy();
      return this;
    }

    @Override
    public boolean isAlive() {
      return alive;
    }
  }

  private static final class Placement {
    private final int x;
    private final int y;
    private final Stone color;

    private Placement(int x, int y, Stone color) {
      this.x = x;
      this.y = y;
      this.color = color;
    }
  }

  private static final class TestEnvironment implements AutoCloseable {
    private final int previousBoardWidth;
    private final int previousBoardHeight;
    private final Config previousConfig;
    private final Board previousBoard;

    private TestEnvironment(
        int previousBoardWidth,
        int previousBoardHeight,
        Config previousConfig,
        Board previousBoard) {
      this.previousBoardWidth = previousBoardWidth;
      this.previousBoardHeight = previousBoardHeight;
      this.previousConfig = previousConfig;
      this.previousBoard = previousBoard;
    }

    private static TestEnvironment open() throws Exception {
      int previousBoardWidth = Board.boardWidth;
      int previousBoardHeight = Board.boardHeight;
      Config previousConfig = Lizzie.config;
      Board previousBoard = Lizzie.board;

      Board.boardWidth = BOARD_SIZE;
      Board.boardHeight = BOARD_SIZE;
      Zobrist.init();

      Config config = allocate(Config.class);
      config.analysisUseCurrentRules = false;
      config.analysisSpecificRules = "";
      config.currentKataGoRules = "";
      config.autoLoadKataRules = false;
      config.kataRules = "";
      config.readKomi = true;
      Lizzie.config = config;
      return new TestEnvironment(
          previousBoardWidth, previousBoardHeight, previousConfig, previousBoard);
    }

    @Override
    public void close() {
      Board.boardWidth = previousBoardWidth;
      Board.boardHeight = previousBoardHeight;
      Zobrist.init();
      Lizzie.config = previousConfig;
      Lizzie.board = previousBoard;
    }
  }

  private static final class UnsafeHolder {
    private static final sun.misc.Unsafe UNSAFE;

    static {
      try {
        Field field = sun.misc.Unsafe.class.getDeclaredField("theUnsafe");
        field.setAccessible(true);
        UNSAFE = (sun.misc.Unsafe) field.get(null);
      } catch (ReflectiveOperationException e) {
        throw new ExceptionInInitializerError(e);
      }
    }
  }
}
