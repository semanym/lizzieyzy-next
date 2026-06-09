package featurecat.lizzie.analysis;

import featurecat.lizzie.Config;
import featurecat.lizzie.Lizzie;
import featurecat.lizzie.rules.Board;
import featurecat.lizzie.rules.BoardData;
import featurecat.lizzie.rules.BoardHistoryNode;
import featurecat.lizzie.util.CommandLaunchHelper;
import featurecat.lizzie.util.KataGoRuntimeHelper;
import featurecat.lizzie.util.Utils;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONArray;
import org.json.JSONObject;

/** Runs KataGo HumanSL analysis queries without changing the normal analysis engine. */
public class HumanSlAnalysisRunner implements AutoCloseable {
  private static final String GTP_COLUMNS = "ABCDEFGHJKLMNOPQRSTUVWXYZ";

  private final List<String> commandParts;
  private final ProcessStarter processStarter;
  private final AtomicInteger nextRequestId = new AtomicInteger(1);
  private final ConcurrentMap<String, CompletableFuture<JSONObject>> pendingResponses =
      new ConcurrentHashMap<String, CompletableFuture<JSONObject>>();

  private Process process;
  private BufferedReader inputStream;
  private BufferedOutputStream outputStream;
  private ScheduledExecutorService readerExecutor;
  private volatile boolean started;
  private volatile boolean closed;
  private volatile String unavailableReason;

  public HumanSlAnalysisRunner(String analysisCommand, Path humanModelPath) {
    this(buildHumanSlCommand(analysisCommand, humanModelPath), ProcessBuilder::start);
  }

  HumanSlAnalysisRunner(List<String> commandParts, ProcessStarter processStarter) {
    this.commandParts = new ArrayList<String>(commandParts);
    this.processStarter = processStarter;
  }

  public synchronized boolean start() {
    if (started && process != null && process.isAlive()) {
      return true;
    }
    if (commandParts.isEmpty()) {
      unavailableReason = "HumanSL analysis command is empty.";
      return false;
    }

    CommandLaunchHelper.LaunchSpec launchSpec = CommandLaunchHelper.prepare(commandParts);
    List<String> preparedCommands = launchSpec.getCommandParts();
    Path engineExecutable = KataGoRuntimeHelper.resolveCommandExecutable(preparedCommands);
    if (Config.isBundledKataGoCommand(String.join(" ", preparedCommands))) {
      try {
        KataGoRuntimeHelper.ensureBundledRuntimeReady(engineExecutable, Lizzie.frame);
      } catch (IOException e) {
        unavailableReason = e.getLocalizedMessage();
        return false;
      }
    }

    List<String> launchCommands =
        KataGoRuntimeHelper.prepareBundledLaunchCommand(preparedCommands, engineExecutable);
    ProcessBuilder processBuilder = new ProcessBuilder(launchCommands);
    CommandLaunchHelper.configureProcessBuilder(processBuilder, launchSpec);
    KataGoRuntimeHelper.configureBundledProcessBuilder(processBuilder, engineExecutable);
    processBuilder.redirectErrorStream(true);
    try {
      process = processStarter.start(processBuilder);
      inputStream =
          new BufferedReader(
              new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
      outputStream = new BufferedOutputStream(process.getOutputStream());
      readerExecutor = Executors.newSingleThreadScheduledExecutor();
      readerExecutor.execute(this::readLoop);
      started = true;
      unavailableReason = null;
      return true;
    } catch (IOException e) {
      unavailableReason = e.getLocalizedMessage();
      close();
      return false;
    }
  }

  public HumanSlBatchResult analyzeMainline(
      BoardHistoryNode endNode,
      List<String> profiles,
      Duration timeout,
      CancellationToken cancellationToken) {
    if (profiles == null || profiles.isEmpty()) {
      return HumanSlBatchResult.unavailable("No HumanSL profiles configured.");
    }
    if (endNode == null) {
      return HumanSlBatchResult.unavailable("No board history node to analyze.");
    }
    if (!ensureStarted()) {
      return HumanSlBatchResult.unavailable(unavailableReason);
    }

    CancellationToken token =
        cancellationToken == null ? CancellationToken.neverCancel() : cancellationToken;
    Duration effectiveTimeout = timeout == null ? Duration.ofSeconds(30) : timeout;
    List<MoveProfileResult> results = new ArrayList<MoveProfileResult>();
    for (BoardHistoryNode moveNode : collectMainlineActionNodes(endNode)) {
      if (token.isCancelled()) {
        results.add(MoveProfileResult.cancelled(moveNode.getData().moveNumber));
        break;
      }
      for (String profile : profiles) {
        results.add(queryMoveProfile(moveNode, profile, effectiveTimeout, token));
      }
    }
    return HumanSlBatchResult.available(results);
  }

  public MoveProfileResult queryMoveProfile(
      BoardHistoryNode moveNode,
      String profile,
      Duration timeout,
      CancellationToken cancellationToken) {
    if (cancellationToken != null && cancellationToken.isCancelled()) {
      return MoveProfileResult.cancelled(moveNumber(moveNode));
    }
    if (!ensureStarted()) {
      return MoveProfileResult.failure(
          moveNumber(moveNode), profile, null, ResultStatus.ENGINE_UNAVAILABLE, unavailableReason);
    }
    if (moveNode == null || !isRealAction(moveNode.getData()) || !moveNode.previous().isPresent()) {
      return MoveProfileResult.failure(
          moveNumber(moveNode), profile, null, ResultStatus.INVALID_MOVE, "No real mainline move.");
    }

    String actualMove = actualMoveName(moveNode.getData());
    String requestId = "humansl-" + nextRequestId.getAndIncrement();
    JSONObject request = buildHumanSlRequest(requestId, moveNode.previous().get(), profile);
    try {
      JSONObject response = request(request, timeout == null ? Duration.ofSeconds(30) : timeout);
      Object policy = extractHumanPolicy(response);
      if (policy == null) {
        return MoveProfileResult.failure(
            moveNode.getData().moveNumber,
            profile,
            actualMove,
            ResultStatus.MISSING_HUMAN_POLICY,
            "KataGo response does not contain humanPolicy.");
      }
      Double probability = extractMoveProbability(policy, actualMove, Board.boardWidth);
      if (probability == null) {
        return MoveProfileResult.failure(
            moveNode.getData().moveNumber,
            profile,
            actualMove,
            ResultStatus.ILLEGAL_OR_MISSING_MOVE,
            "humanPolicy does not contain the actual move.");
      }
      return MoveProfileResult.success(
          moveNode.getData().moveNumber, profile, actualMove, probability.doubleValue());
    } catch (TimeoutException e) {
      return MoveProfileResult.failure(
          moveNode.getData().moveNumber, profile, actualMove, ResultStatus.TIMEOUT, e.getMessage());
    } catch (IOException e) {
      return MoveProfileResult.failure(
          moveNode.getData().moveNumber,
          profile,
          actualMove,
          ResultStatus.QUERY_FAILED,
          e.getLocalizedMessage());
    }
  }

  /**
   * Picks the move a player of the given HumanSL profile would most likely play in the position
   * represented by {@code positionNode}. Returns the GTP move name (e.g. "Q16"), "pass", or empty
   * if no move could be obtained.
   */
  public Optional<String> bestHumanMove(
      BoardHistoryNode positionNode, String profile, Duration timeout) {
    if (positionNode == null || profile == null) {
      return Optional.empty();
    }
    if (!ensureStarted()) {
      return Optional.empty();
    }
    String requestId = "humansl-genmove-" + nextRequestId.getAndIncrement();
    JSONObject request = buildHumanSlRequest(requestId, positionNode, profile);
    try {
      JSONObject response = request(request, timeout == null ? Duration.ofSeconds(30) : timeout);
      Object policy = extractHumanPolicy(response);
      if (policy == null) {
        return Optional.empty();
      }
      return Optional.ofNullable(argmaxPolicyMove(policy, Board.boardWidth, Board.boardHeight));
    } catch (TimeoutException | IOException e) {
      return Optional.empty();
    }
  }

  static String argmaxPolicyMove(Object policy, int boardWidth, int boardHeight) {
    if (policy == null) {
      return null;
    }
    if (policy instanceof JSONArray) {
      JSONArray array = (JSONArray) policy;
      if (isNumericPolicy(array)) {
        int bestIndex = -1;
        double bestValue = Double.NEGATIVE_INFINITY;
        for (int i = 0; i < array.length(); i++) {
          Double value = coerceProbability(array.opt(i));
          if (value != null && value.doubleValue() > bestValue) {
            bestValue = value.doubleValue();
            bestIndex = i;
          }
        }
        if (bestIndex < 0) {
          return null;
        }
        if (bestIndex == boardWidth * boardHeight) {
          return "pass";
        }
        int[] coords = Board.getCoord(bestIndex);
        return Board.convertCoordinatesToName(coords[0], coords[1]);
      }
      String bestMove = null;
      double bestValue = Double.NEGATIVE_INFINITY;
      for (int i = 0; i < array.length(); i++) {
        Object item = array.opt(i);
        if (!(item instanceof JSONArray)) {
          continue;
        }
        JSONArray pair = (JSONArray) item;
        if (pair.length() < 2) {
          continue;
        }
        Double value = coerceProbability(pair.opt(1));
        if (value != null && value.doubleValue() > bestValue) {
          bestValue = value.doubleValue();
          bestMove = pair.optString(0);
        }
      }
      return bestMove;
    }
    if (policy instanceof JSONObject) {
      JSONObject object = (JSONObject) policy;
      String bestMove = null;
      double bestValue = Double.NEGATIVE_INFINITY;
      for (String key : object.keySet()) {
        Double value = coerceProbability(object.opt(key));
        if (value != null && value.doubleValue() > bestValue) {
          bestValue = value.doubleValue();
          bestMove = key;
        }
      }
      return bestMove;
    }
    return null;
  }

  public JSONObject request(JSONObject request, Duration timeout)
      throws IOException, TimeoutException {
    if (!started || outputStream == null) {
      throw new IOException("HumanSL analysis engine is not started.");
    }
    String id = request.optString("id", "");
    if (id.isEmpty()) {
      throw new IOException("HumanSL request id is empty.");
    }
    CompletableFuture<JSONObject> future = new CompletableFuture<JSONObject>();
    pendingResponses.put(id, future);
    try {
      outputStream.write((request.toString() + "\n").getBytes(StandardCharsets.UTF_8));
      outputStream.flush();
      long timeoutMillis = Math.max(1L, timeout.toMillis());
      return future.get(timeoutMillis, TimeUnit.MILLISECONDS);
    } catch (java.util.concurrent.TimeoutException e) {
      throw new TimeoutException("Timed out waiting for HumanSL response " + id + ".");
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new IOException("Interrupted while waiting for HumanSL response.", e);
    } catch (java.util.concurrent.ExecutionException e) {
      Throwable cause = e.getCause();
      if (cause instanceof IOException) {
        throw (IOException) cause;
      }
      throw new IOException(cause);
    } finally {
      pendingResponses.remove(id);
    }
  }

  public String getUnavailableReason() {
    return unavailableReason;
  }

  public boolean isStarted() {
    return started;
  }

  @Override
  public synchronized void close() {
    closed = true;
    started = false;
    IOException closeError = new IOException("HumanSL analysis runner closed.");
    for (CompletableFuture<JSONObject> future : pendingResponses.values()) {
      future.completeExceptionally(closeError);
    }
    pendingResponses.clear();
    if (readerExecutor != null) {
      readerExecutor.shutdownNow();
    }
    try {
      if (outputStream != null) {
        outputStream.close();
      }
    } catch (IOException ignored) {
    }
    try {
      if (inputStream != null) {
        inputStream.close();
      }
    } catch (IOException ignored) {
    }
    if (process != null && process.isAlive()) {
      process.destroyForcibly();
    }
  }

  static List<String> buildHumanSlCommand(String analysisCommand, Path humanModelPath) {
    List<String> parts = Utils.splitCommand(analysisCommand == null ? "" : analysisCommand.trim());
    for (int i = 0; i < parts.size(); i++) {
      if ("gtp".equalsIgnoreCase(parts.get(i))) {
        parts.set(i, "analysis");
        break;
      }
    }
    String modelPath =
        humanModelPath == null ? "" : humanModelPath.toAbsolutePath().normalize().toString();
    int humanModelIndex = findHumanModelValueIndex(parts);
    if (humanModelIndex >= 0) {
      parts.set(humanModelIndex, modelPath);
    } else if (!modelPath.isEmpty()) {
      parts.add("-human-model");
      parts.add(modelPath);
    }
    return parts;
  }

  static JSONObject buildHumanSlRequest(String id, BoardHistoryNode positionNode, String profile) {
    JSONObject request =
        AnalysisRequestBuilder.buildRequest(id, positionNode, 1, false, false, false);
    request.put("includePolicy", true);
    request.put("maxVisits", 1);
    JSONObject overrideSettings = request.optJSONObject("overrideSettings");
    if (overrideSettings == null) {
      overrideSettings = new JSONObject();
    }
    overrideSettings.put("humanSLProfile", profile);
    request.put("overrideSettings", overrideSettings);
    return request;
  }

  static Object extractHumanPolicy(JSONObject response) {
    if (response.has("humanPolicy")) {
      return response.get("humanPolicy");
    }
    JSONObject rootInfo = response.optJSONObject("rootInfo");
    if (rootInfo != null && rootInfo.has("humanPolicy")) {
      return rootInfo.get("humanPolicy");
    }
    return null;
  }

  static Double extractMoveProbability(Object policy, String move, int boardSize) {
    if (policy == null || move == null) {
      return null;
    }
    String normalizedMove = move.trim().toUpperCase(Locale.ROOT);
    if (policy instanceof JSONObject) {
      JSONObject object = (JSONObject) policy;
      Object value =
          object.has(normalizedMove)
              ? object.opt(normalizedMove)
              : object.opt(normalizedMove.toLowerCase(Locale.ROOT));
      return coerceProbability(value);
    }
    if (policy instanceof JSONArray) {
      JSONArray array = (JSONArray) policy;
      if (isNumericPolicy(array)) {
        int index = gtpPolicyIndex(normalizedMove, boardSize);
        if (index < 0 || index >= array.length()) {
          return null;
        }
        return coerceProbability(array.opt(index));
      }
      for (int i = 0; i < array.length(); i++) {
        Object item = array.opt(i);
        if (!(item instanceof JSONArray)) {
          continue;
        }
        JSONArray pair = (JSONArray) item;
        if (pair.length() >= 2 && normalizedMove.equalsIgnoreCase(pair.optString(0))) {
          return coerceProbability(pair.opt(1));
        }
      }
    }
    return null;
  }

  private boolean ensureStarted() {
    return started || start();
  }

  private void readLoop() {
    try {
      String line;
      while (!closed && (line = inputStream.readLine()) != null) {
        if (!line.trim().startsWith("{")) {
          continue;
        }
        JSONObject response = new JSONObject(line);
        String id = response.optString("id", "");
        CompletableFuture<JSONObject> future = pendingResponses.get(id);
        if (future != null) {
          future.complete(response);
        }
      }
    } catch (Exception e) {
      IOException ioException = new IOException("HumanSL analysis reader stopped.", e);
      for (CompletableFuture<JSONObject> future : pendingResponses.values()) {
        future.completeExceptionally(ioException);
      }
    } finally {
      started = false;
    }
  }

  private static List<BoardHistoryNode> collectMainlineActionNodes(BoardHistoryNode endNode) {
    ArrayList<BoardHistoryNode> reversed = new ArrayList<BoardHistoryNode>();
    BoardHistoryNode current = endNode;
    while (current != null && current.previous().isPresent()) {
      if (isRealAction(current.getData())) {
        reversed.add(current);
      }
      Optional<BoardHistoryNode> previous = current.previous();
      current = previous.isPresent() ? previous.get() : null;
    }
    Collections.reverse(reversed);
    return reversed;
  }

  private static boolean isRealAction(BoardData data) {
    return data != null && (data.isMoveNode() || (data.isPassNode() && !data.dummy));
  }

  private static String actualMoveName(BoardData data) {
    if (data.isPassNode()) {
      return "pass";
    }
    int[] move = data.lastMove.get();
    return Board.convertCoordinatesToName(move[0], move[1]);
  }

  private static int moveNumber(BoardHistoryNode node) {
    return node == null ? -1 : node.getData().moveNumber;
  }

  private static int findHumanModelValueIndex(List<String> parts) {
    for (int i = 0; i < parts.size() - 1; i++) {
      String part = parts.get(i);
      if ("-human-model".equals(part) || "--human-model".equals(part)) {
        return i + 1;
      }
    }
    return -1;
  }

  private static boolean isNumericPolicy(JSONArray array) {
    if (array.length() == 0) {
      return false;
    }
    for (int i = 0; i < array.length(); i++) {
      Object value = array.opt(i);
      if (!(value instanceof Number)) {
        return false;
      }
    }
    return true;
  }

  private static Double coerceProbability(Object value) {
    if (!(value instanceof Number)) {
      return null;
    }
    double probability = ((Number) value).doubleValue();
    if (Double.isNaN(probability) || probability < 0.0) {
      return null;
    }
    return Math.max(probability, 1.0e-12);
  }

  private static int gtpPolicyIndex(String move, int boardSize) {
    if ("PASS".equals(move)) {
      return boardSize * boardSize;
    }
    int[] coords = Board.convertNameToCoordinates(move, boardSize);
    if (coords == null || coords.length < 2 || !Board.isValid(coords[0], coords[1])) {
      return -1;
    }
    return Board.getIndex(coords[0], coords[1]);
  }

  interface ProcessStarter {
    Process start(ProcessBuilder processBuilder) throws IOException;
  }

  public interface CancellationToken {
    boolean isCancelled();

    static CancellationToken neverCancel() {
      return () -> false;
    }
  }

  public enum ResultStatus {
    OK,
    ENGINE_UNAVAILABLE,
    TIMEOUT,
    MISSING_HUMAN_POLICY,
    ILLEGAL_OR_MISSING_MOVE,
    INVALID_MOVE,
    QUERY_FAILED,
    CANCELLED
  }

  public static final class HumanSlBatchResult {
    private final boolean available;
    private final String unavailableReason;
    private final List<MoveProfileResult> results;

    private HumanSlBatchResult(
        boolean available, String unavailableReason, List<MoveProfileResult> results) {
      this.available = available;
      this.unavailableReason = unavailableReason;
      this.results = results;
    }

    static HumanSlBatchResult available(List<MoveProfileResult> results) {
      return new HumanSlBatchResult(true, null, new ArrayList<MoveProfileResult>(results));
    }

    static HumanSlBatchResult unavailable(String reason) {
      return new HumanSlBatchResult(false, reason, Collections.<MoveProfileResult>emptyList());
    }

    public boolean isAvailable() {
      return available;
    }

    public String getUnavailableReason() {
      return unavailableReason;
    }

    public List<MoveProfileResult> getResults() {
      return new ArrayList<MoveProfileResult>(results);
    }
  }

  public static final class MoveProfileResult {
    private final int moveNumber;
    private final String profile;
    private final String move;
    private final ResultStatus status;
    private final Double probability;
    private final String message;

    private MoveProfileResult(
        int moveNumber,
        String profile,
        String move,
        ResultStatus status,
        Double probability,
        String message) {
      this.moveNumber = moveNumber;
      this.profile = profile;
      this.move = move;
      this.status = status;
      this.probability = probability;
      this.message = message;
    }

    static MoveProfileResult success(
        int moveNumber, String profile, String move, double probability) {
      return new MoveProfileResult(moveNumber, profile, move, ResultStatus.OK, probability, null);
    }

    static MoveProfileResult failure(
        int moveNumber, String profile, String move, ResultStatus status, String message) {
      return new MoveProfileResult(moveNumber, profile, move, status, null, message);
    }

    static MoveProfileResult cancelled(int moveNumber) {
      return new MoveProfileResult(
          moveNumber, null, null, ResultStatus.CANCELLED, null, "HumanSL analysis cancelled.");
    }

    public int getMoveNumber() {
      return moveNumber;
    }

    public String getProfile() {
      return profile;
    }

    public String getMove() {
      return move;
    }

    public ResultStatus getStatus() {
      return status;
    }

    public Optional<Double> getProbability() {
      return Optional.ofNullable(probability);
    }

    public String getMessage() {
      return message;
    }
  }

  public Map<String, Object> describe() {
    Map<String, Object> description = new LinkedHashMap<String, Object>();
    description.put("started", started);
    description.put("command", new ArrayList<String>(commandParts));
    description.put("pendingResponses", pendingResponses.size());
    description.put("unavailableReason", unavailableReason);
    return description;
  }
}
