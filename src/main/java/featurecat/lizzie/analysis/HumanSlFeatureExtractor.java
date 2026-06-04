package featurecat.lizzie.analysis;

import featurecat.lizzie.analysis.HumanSlAnalysisRunner.HumanSlBatchResult;
import featurecat.lizzie.analysis.HumanSlAnalysisRunner.MoveProfileResult;
import featurecat.lizzie.analysis.HumanSlAnalysisRunner.ResultStatus;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** Extracts explainable HumanSL rank-similarity features from per-move policies. */
public final class HumanSlFeatureExtractor {
  public static final double MIN_PROBABILITY = 1.0e-12;
  public static final List<String> DEFAULT_PROFILES = buildDefaultProfiles();

  private static final List<String> LOW_RANK_PROFILES =
      List.of("rank_18k", "rank_17k", "rank_16k", "rank_15k", "rank_14k", "rank_13k");
  private static final List<String> HIGH_RANK_PROFILES = List.of("rank_5d", "rank_7d", "rank_9d");

  private HumanSlFeatureExtractor() {}

  private static List<String> buildDefaultProfiles() {
    List<String> profiles = new ArrayList<String>();
    for (int rank = 18; rank >= 1; rank--) {
      profiles.add("rank_" + rank + "k");
    }
    for (int rank = 1; rank <= 9; rank++) {
      profiles.add("rank_" + rank + "d");
    }
    return Collections.unmodifiableList(profiles);
  }

  public static FeatureReport extract(HumanSlBatchResult batchResult) {
    if (batchResult == null || !batchResult.isAvailable()) {
      return FeatureReport.empty();
    }
    return extract(batchResult.getResults(), DEFAULT_PROFILES);
  }

  public static FeatureReport extract(List<MoveProfileResult> results) {
    return extract(results, DEFAULT_PROFILES);
  }

  public static FeatureReport extract(List<MoveProfileResult> results, List<String> profiles) {
    List<String> profileOrder =
        profiles == null || profiles.isEmpty()
            ? DEFAULT_PROFILES
            : Collections.unmodifiableList(new ArrayList<String>(profiles));
    FeatureBuilder overall = new FeatureBuilder(profileOrder);
    FeatureBuilder black = new FeatureBuilder(profileOrder);
    FeatureBuilder white = new FeatureBuilder(profileOrder);

    if (results != null) {
      int maxMoveNumber = maxMoveNumber(results);
      for (MoveProfileResult result : results) {
        if (result == null || result.getProfile() == null || result.getMoveNumber() <= 0) {
          continue;
        }
        double probability = probabilityOrFloor(result);
        boolean anomalous = result.getStatus() != ResultStatus.OK || probability <= MIN_PROBABILITY;
        HumanSlStage stage = stageOf(result.getMoveNumber(), maxMoveNumber);
        overall.add(result.getProfile(), probability, stage, anomalous);
        if (sideOf(result.getMoveNumber()) == HumanSlSide.BLACK) {
          black.add(result.getProfile(), probability, stage, anomalous);
        } else {
          white.add(result.getProfile(), probability, stage, anomalous);
        }
      }
    }

    return new FeatureReport(overall.build(), black.build(), white.build());
  }

  private static double probabilityOrFloor(MoveProfileResult result) {
    Optional<Double> probability = result.getProbability();
    if (!probability.isPresent()) {
      return MIN_PROBABILITY;
    }
    double value = probability.get().doubleValue();
    if (!Double.isFinite(value) || value <= 0.0) {
      return MIN_PROBABILITY;
    }
    return Math.max(value, MIN_PROBABILITY);
  }

  private static int maxMoveNumber(List<MoveProfileResult> results) {
    int max = 0;
    for (MoveProfileResult result : results) {
      if (result != null) {
        max = Math.max(max, result.getMoveNumber());
      }
    }
    return max;
  }

  private static HumanSlSide sideOf(int moveNumber) {
    return moveNumber % 2 == 1 ? HumanSlSide.BLACK : HumanSlSide.WHITE;
  }

  private static HumanSlStage stageOf(int moveNumber, int maxMoveNumber) {
    if (maxMoveNumber <= 1) {
      return HumanSlStage.OPENING;
    }
    double ratio = moveNumber / (double) maxMoveNumber;
    if (ratio <= 1.0 / 3.0) {
      return HumanSlStage.OPENING;
    }
    if (ratio <= 2.0 / 3.0) {
      return HumanSlStage.MIDDLE;
    }
    return HumanSlStage.ENDGAME;
  }

  public enum HumanSlSide {
    OVERALL,
    BLACK,
    WHITE
  }

  public enum HumanSlStage {
    OPENING,
    MIDDLE,
    ENDGAME
  }

  public static final class FeatureReport {
    public final SideFeatures overall;
    public final SideFeatures black;
    public final SideFeatures white;

    private FeatureReport(SideFeatures overall, SideFeatures black, SideFeatures white) {
      this.overall = overall;
      this.black = black;
      this.white = white;
    }

    private static FeatureReport empty() {
      SideFeatures empty = new FeatureBuilder(DEFAULT_PROFILES).build();
      return new FeatureReport(empty, empty, empty);
    }
  }

  public static final class SideFeatures {
    public final int sampleCount;
    public final int anomalousSampleCount;
    public final Map<String, Double> averageLogProbabilityByProfile;
    public final String bestProfile;
    public final double bestSecondGap;
    public final double highLowTrend;
    public final Map<HumanSlStage, StageFeatures> stageFeatures;

    private SideFeatures(
        int sampleCount,
        int anomalousSampleCount,
        Map<String, Double> averageLogProbabilityByProfile,
        String bestProfile,
        double bestSecondGap,
        double highLowTrend,
        Map<HumanSlStage, StageFeatures> stageFeatures) {
      this.sampleCount = sampleCount;
      this.anomalousSampleCount = anomalousSampleCount;
      this.averageLogProbabilityByProfile =
          Collections.unmodifiableMap(
              new LinkedHashMap<String, Double>(averageLogProbabilityByProfile));
      this.bestProfile = bestProfile;
      this.bestSecondGap = bestSecondGap;
      this.highLowTrend = highLowTrend;
      this.stageFeatures =
          Collections.unmodifiableMap(new EnumMap<HumanSlStage, StageFeatures>(stageFeatures));
    }
  }

  public static final class StageFeatures {
    public final int sampleCount;
    public final Map<String, Double> averageLogProbabilityByProfile;
    public final String bestProfile;

    private StageFeatures(
        int sampleCount, Map<String, Double> averageLogProbabilityByProfile, String bestProfile) {
      this.sampleCount = sampleCount;
      this.averageLogProbabilityByProfile =
          Collections.unmodifiableMap(
              new LinkedHashMap<String, Double>(averageLogProbabilityByProfile));
      this.bestProfile = bestProfile;
    }
  }

  private static final class FeatureBuilder {
    private final List<String> profiles;
    private final Map<String, LogAccumulator> profileStats =
        new LinkedHashMap<String, LogAccumulator>();
    private final Map<HumanSlStage, Map<String, LogAccumulator>> stageStats =
        new EnumMap<HumanSlStage, Map<String, LogAccumulator>>(HumanSlStage.class);
    private int sampleCount;
    private int anomalousSampleCount;

    private FeatureBuilder(List<String> profiles) {
      this.profiles = profiles;
      for (String profile : profiles) {
        profileStats.put(profile, new LogAccumulator());
      }
      for (HumanSlStage stage : HumanSlStage.values()) {
        Map<String, LogAccumulator> perStage = new LinkedHashMap<String, LogAccumulator>();
        for (String profile : profiles) {
          perStage.put(profile, new LogAccumulator());
        }
        stageStats.put(stage, perStage);
      }
    }

    private void add(String profile, double probability, HumanSlStage stage, boolean anomalous) {
      if (!profileStats.containsKey(profile)) {
        return;
      }
      double logProbability = Math.log(Math.max(probability, MIN_PROBABILITY));
      profileStats.get(profile).add(logProbability);
      stageStats.get(stage).get(profile).add(logProbability);
      sampleCount++;
      if (anomalous) {
        anomalousSampleCount++;
      }
    }

    private SideFeatures build() {
      Map<String, Double> averages = averages(profileStats);
      Map<HumanSlStage, StageFeatures> stages =
          new EnumMap<HumanSlStage, StageFeatures>(HumanSlStage.class);
      for (HumanSlStage stage : HumanSlStage.values()) {
        Map<String, Double> stageAverages = averages(stageStats.get(stage));
        stages.put(
            stage,
            new StageFeatures(
                sampleCount(stageStats.get(stage)), stageAverages, bestProfile(stageAverages)));
      }
      return new SideFeatures(
          sampleCount,
          anomalousSampleCount,
          averages,
          bestProfile(averages),
          bestSecondGap(averages),
          highLowTrend(averages),
          stages);
    }

    private Map<String, Double> averages(Map<String, LogAccumulator> stats) {
      Map<String, Double> averages = new LinkedHashMap<String, Double>();
      for (String profile : profiles) {
        LogAccumulator accumulator = stats.get(profile);
        if (accumulator != null && accumulator.count > 0) {
          averages.put(profile, accumulator.average());
        }
      }
      return averages;
    }

    private int sampleCount(Map<String, LogAccumulator> stats) {
      int count = 0;
      for (LogAccumulator accumulator : stats.values()) {
        count += accumulator.count;
      }
      return count;
    }

    private String bestProfile(Map<String, Double> averages) {
      String bestProfile = null;
      double bestValue = Double.NEGATIVE_INFINITY;
      for (Map.Entry<String, Double> entry : averages.entrySet()) {
        if (entry.getValue().doubleValue() > bestValue) {
          bestProfile = entry.getKey();
          bestValue = entry.getValue().doubleValue();
        }
      }
      return bestProfile;
    }

    private double bestSecondGap(Map<String, Double> averages) {
      double best = Double.NEGATIVE_INFINITY;
      double second = Double.NEGATIVE_INFINITY;
      for (double value : averages.values()) {
        if (value > best) {
          second = best;
          best = value;
        } else if (value > second) {
          second = value;
        }
      }
      if (!Double.isFinite(best) || !Double.isFinite(second)) {
        return 0.0;
      }
      return best - second;
    }

    private double highLowTrend(Map<String, Double> averages) {
      Optional<Double> highAverage = averageProfiles(averages, HIGH_RANK_PROFILES);
      Optional<Double> lowAverage = averageProfiles(averages, LOW_RANK_PROFILES);
      if (!highAverage.isPresent() || !lowAverage.isPresent()) {
        return 0.0;
      }
      return highAverage.get().doubleValue() - lowAverage.get().doubleValue();
    }

    private Optional<Double> averageProfiles(
        Map<String, Double> averages, List<String> selectedProfiles) {
      double sum = 0.0;
      int count = 0;
      for (String profile : selectedProfiles) {
        Double value = averages.get(profile);
        if (value != null) {
          sum += value.doubleValue();
          count++;
        }
      }
      if (count == 0) {
        return Optional.empty();
      }
      return Optional.of(sum / count);
    }
  }

  private static final class LogAccumulator {
    private double sum;
    private int count;

    private void add(double value) {
      sum += value;
      count++;
    }

    private double average() {
      return sum / count;
    }
  }
}
