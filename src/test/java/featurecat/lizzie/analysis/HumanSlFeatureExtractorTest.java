package featurecat.lizzie.analysis;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import featurecat.lizzie.analysis.HumanSlAnalysisRunner.HumanSlBatchResult;
import featurecat.lizzie.analysis.HumanSlAnalysisRunner.MoveProfileResult;
import featurecat.lizzie.analysis.HumanSlAnalysisRunner.ResultStatus;
import featurecat.lizzie.analysis.HumanSlFeatureExtractor.FeatureReport;
import featurecat.lizzie.analysis.HumanSlFeatureExtractor.HumanSlStage;
import featurecat.lizzie.analysis.HumanSlFeatureExtractor.SideFeatures;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class HumanSlFeatureExtractorTest {

  @Test
  void extract_summarizesDefaultProfilesBestGapTrendAndStages() {
    HumanSlBatchResult batch = HumanSlBatchResult.available(profileResults());

    FeatureReport report = HumanSlFeatureExtractor.extract(batch);
    SideFeatures overall = report.overall;

    assertEquals(HumanSlFeatureExtractor.DEFAULT_PROFILES.size() * 3, overall.sampleCount);
    assertEquals(0, overall.anomalousSampleCount);
    assertEquals(
        HumanSlFeatureExtractor.DEFAULT_PROFILES.size(),
        overall.averageLogProbabilityByProfile.size());
    assertEquals("rank_5d", overall.bestProfile);
    assertEquals(
        (Math.log(0.40) + Math.log(0.55) + Math.log(0.40)) / 3.0
            - (Math.log(0.50) + Math.log(0.45) + Math.log(0.30)) / 3.0,
        overall.bestSecondGap,
        0.000001);
    assertTrue(overall.highLowTrend > 0.0);
    assertEquals("rank_9d", overall.stageFeatures.get(HumanSlStage.OPENING).bestProfile);
    assertEquals("rank_5d", overall.stageFeatures.get(HumanSlStage.MIDDLE).bestProfile);
    assertEquals("rank_1d", overall.stageFeatures.get(HumanSlStage.ENDGAME).bestProfile);
  }

  @Test
  void extract_splitsMoveSamplesBySide() {
    HumanSlBatchResult batch = HumanSlBatchResult.available(profileResults());

    FeatureReport report = HumanSlFeatureExtractor.extract(batch);

    assertEquals(HumanSlFeatureExtractor.DEFAULT_PROFILES.size() * 2, report.black.sampleCount);
    assertEquals(HumanSlFeatureExtractor.DEFAULT_PROFILES.size(), report.white.sampleCount);
    assertEquals("rank_9d", report.black.bestProfile);
    assertEquals("rank_5d", report.white.bestProfile);
  }

  @Test
  void extract_usesFloorLogProbabilityForMissingOrIllegalProbabilities() {
    List<MoveProfileResult> results =
        List.of(
            MoveProfileResult.failure(
                1,
                "rank_1d",
                "A1",
                ResultStatus.ILLEGAL_OR_MISSING_MOVE,
                "humanPolicy does not contain the actual move."),
            MoveProfileResult.success(1, "rank_3d", "A1", 0.25));

    FeatureReport report = HumanSlFeatureExtractor.extract(results);

    assertEquals(2, report.overall.sampleCount);
    assertEquals(1, report.overall.anomalousSampleCount);
    assertEquals(
        Math.log(HumanSlFeatureExtractor.MIN_PROBABILITY),
        report.overall.averageLogProbabilityByProfile.get("rank_1d"),
        0.000001);
    assertEquals("rank_3d", report.overall.bestProfile);
  }

  @Test
  void extract_marksZeroNanAndInfiniteProbabilitiesAsAnomalousFloorSamples() {
    List<MoveProfileResult> results =
        List.of(
            MoveProfileResult.success(1, "rank_10k", "A1", 0.0),
            MoveProfileResult.success(1, "rank_5k", "A1", Double.NaN),
            MoveProfileResult.success(1, "rank_1k", "A1", Double.POSITIVE_INFINITY),
            MoveProfileResult.success(1, "rank_1d", "A1", 0.2));

    FeatureReport report = HumanSlFeatureExtractor.extract(results);

    assertEquals(4, report.overall.sampleCount);
    assertEquals(3, report.overall.anomalousSampleCount);
    assertEquals(
        Math.log(HumanSlFeatureExtractor.MIN_PROBABILITY),
        report.overall.averageLogProbabilityByProfile.get("rank_10k"),
        0.000001);
    assertEquals(
        Math.log(HumanSlFeatureExtractor.MIN_PROBABILITY),
        report.overall.averageLogProbabilityByProfile.get("rank_5k"),
        0.000001);
    assertEquals(
        Math.log(HumanSlFeatureExtractor.MIN_PROBABILITY),
        report.overall.averageLogProbabilityByProfile.get("rank_1k"),
        0.000001);
    assertEquals("rank_1d", report.overall.bestProfile);
  }

  @Test
  void extract_usesConfiguredProfileBoundaryAndIgnoresOutOfScopeResults() {
    List<MoveProfileResult> results =
        List.of(
            MoveProfileResult.success(1, "rank_1d", "A1", 0.2),
            MoveProfileResult.success(1, "rank_9d", "A1", 0.9),
            MoveProfileResult.success(1, "experimental", "A1", 1.0));

    FeatureReport report = HumanSlFeatureExtractor.extract(results, List.of("rank_1d", "rank_9d"));

    assertEquals(2, report.overall.sampleCount);
    assertEquals(2, report.overall.averageLogProbabilityByProfile.size());
    assertFalse(report.overall.averageLogProbabilityByProfile.containsKey("experimental"));
    assertEquals("rank_9d", report.overall.bestProfile);
    assertEquals(Math.log(0.9) - Math.log(0.2), report.overall.bestSecondGap, 0.000001);
  }

  @Test
  void extract_unavailableBatchReturnsEmptyFeatures() {
    FeatureReport report =
        HumanSlFeatureExtractor.extract(HumanSlBatchResult.unavailable("missing"));

    assertEquals(0, report.overall.sampleCount);
    assertEquals(0, report.overall.averageLogProbabilityByProfile.size());
    assertNull(report.overall.bestProfile);
  }

  private static List<MoveProfileResult> profileResults() {
    List<MoveProfileResult> results = new ArrayList<>();
    addMove(
        results,
        1,
        Map.of(
            "rank_10k", 0.15,
            "rank_5k", 0.20,
            "rank_1k", 0.25,
            "rank_1d", 0.30,
            "rank_3d", 0.35,
            "rank_5d", 0.40,
            "rank_7d", 0.50,
            "rank_9d", 0.95));
    addMove(
        results,
        2,
        Map.of(
            "rank_10k", 0.10,
            "rank_5k", 0.15,
            "rank_1k", 0.20,
            "rank_1d", 0.25,
            "rank_3d", 0.30,
            "rank_5d", 0.55,
            "rank_7d", 0.45,
            "rank_9d", 0.35));
    addMove(
        results,
        3,
        Map.of(
            "rank_10k", 0.20,
            "rank_5k", 0.30,
            "rank_1k", 0.45,
            "rank_1d", 0.60,
            "rank_3d", 0.50,
            "rank_5d", 0.40,
            "rank_7d", 0.30,
            "rank_9d", 0.20));
    return results;
  }

  private static void addMove(
      List<MoveProfileResult> results, int moveNumber, Map<String, Double> probabilities) {
    for (String profile : HumanSlFeatureExtractor.DEFAULT_PROFILES) {
      results.add(
          MoveProfileResult.success(
              moveNumber, profile, "A1", probabilities.getOrDefault(profile, 0.01)));
    }
  }
}
